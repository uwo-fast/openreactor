"""Run a profile: step each channel's setpoint on the controller's thread.

The runner is polled on every controller tick, and at each slice poll
period it works out what each channel in the profile should run and sends
what changed. When the profile ends, or is aborted, every channel it
touched goes to its safe state.

A profile never overrides anything else. It is aborted if a channel no
longer runs what the profile last sent it, and on any event that stops or
resets one of its slices, seen through ``notice``: stop-all, an e-stop, a
reboot, a drift the slice was stopped for, a held trip, or the slice going
unreachable. So a profile cannot restart a heater or a motor that something
else stopped, even one it had not set yet.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from crumbs_i2c import CrumbsError

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Event
from openreactor.dcmt import DcmtSlice, duty_to_pwm, pwm_to_duty
from openreactor.ezo import Result
from openreactor.profile import OFF, Profile, Setpoint
from openreactor.rlht import SendFailed, SliceBusy, SliceError
from openreactor.slices import Slice


@dataclass
class _Channel:
    config: ChannelConfig
    device: DeviceConfig
    slice: Slice
    # What the profile last sent, in the slice's units (tenths of a degree,
    # or PWM); None until it first sends.
    sent: int | None = None

    def wanted(self) -> int:
        """Return what the slice is to run on this channel, in its units."""
        if isinstance(self.slice, DcmtSlice):
            return self.slice.pwm[self.config.output - 1]
        return self.slice.setpoints_deci[self.config.output - 1]

    def units(self, value: Setpoint) -> int | None:
        """Return ``value`` in the slice's units, 0 for off, None for nothing yet."""
        if value is None:
            return None
        if value == OFF:
            return 0
        assert isinstance(value, float)
        if isinstance(self.slice, DcmtSlice):
            return duty_to_pwm(value)
        return round(value * 10)

    def describe(self, units: int) -> str:
        if isinstance(self.slice, DcmtSlice):
            return f"{pwm_to_duty(units):g} %"
        return f"{units / 10:g} °C"

    def send(self, units: int) -> None:
        if isinstance(self.slice, DcmtSlice):
            self.slice.set_duty(self.config.output, units)
        else:
            self.slice.set_setpoint(self.config.output, units)

    def forget(self) -> None:
        self.slice.forget(self.config.output)


# Events that stop or reset a slice: on one of the profile's, it is aborted.
_STOPPING = {
    "slice-reboot",
    "slice-watchdog-changed",
    "slice-setpoints-changed",
    "slice-duty-changed",
    "slice-unreachable",
}


class ProfileRunner:
    """One profile running against the configured slices.

    ``finished`` is called on the controller's thread, once, with
    ``"completed"`` or ``"stopped"`` and why, when the profile ends or is
    aborted; the runner is then done and should be removed from the
    controller's polled devices.
    """

    def __init__(
        self,
        profile: Profile,
        channels: dict[str, tuple[DeviceConfig, ChannelConfig, Slice]],
        *,
        step_s: float,
        finished: Callable[[str, str], None],
        wall: Callable[[], float] = time.time,
    ):
        self.profile = profile
        self._channels = {
            name: _Channel(ch, d, s)
            for name, (d, ch, s) in channels.items()
            if name in profile.channels
        }
        # The controller's clock at the first tick, which starts the profile.
        self.start: float | None = None
        self._step_s = step_s
        self._next = 0.0
        self._finished = finished
        self._wall = wall
        self._devices = {ch.device.name for ch in self._channels.values()}
        # Why the profile is to be aborted at its next step, from ``notice``.
        self._stop: str | None = None
        self.done = False

    def elapsed(self, now: float) -> float:
        return 0.0 if self.start is None else max(0.0, now - self.start)

    def _event(
        self, kind: str, details: str, result: str = "ok", channel: _Channel | None = None
    ) -> Event:
        return Event(
            self._wall(),
            "profile",
            kind,
            device=channel.device.name if channel else None,
            channel=channel.config.name if channel else None,
            details=details,
            result=result,
        )

    def notice(self, item: Result | Event) -> None:
        """Watch the controller's events for one that stops one of the slices.

        Call it on the controller's thread, as a listener. It only marks the
        profile to be aborted at its next step, which runs on the same tick.
        """
        if self.done or self._stop is not None or not isinstance(item, Event):
            return
        if item.kind == "stop-all":
            self._stop = "stop-all was sent"
        elif item.device not in self._devices:
            return
        elif item.kind == "e-stop" and item.result == "held":
            self._stop = f"the e-stop on {item.device} was pressed"
        elif item.kind in _STOPPING or (item.kind == "slice-trip" and item.result == "held"):
            self._stop = f"{item.device}: {item.kind}, {item.result}"

    def advance(self, now: float) -> list[Result | Event]:
        if self.done:
            return []
        if self._stop is not None:
            return self.abort(self._stop)
        if self.start is None:
            self.start = self._next = now
        if now < self._next:
            return []
        self._next += self._step_s
        if self._next <= now:
            self._next = now + self._step_s
        t = now - self.start
        out: list[Result | Event] = []
        for name, ch in self._channels.items():
            if ch.sent is not None and ch.wanted() != ch.sent:
                return out + self.abort(
                    f"{name} no longer runs what the profile set: stop-all, an e-stop, a "
                    "reboot, a stopped drift or another setpoint changed it"
                )
        if t >= self.profile.duration:
            return out + self._finish()
        for name, ch in self._channels.items():
            units = ch.units(self.profile.value_at(name, t))
            if units is None or units == ch.sent:
                continue
            try:
                ch.send(units)
            except SliceBusy:
                continue  # the slice is being checked; try at the next step
            except SendFailed as e:
                out.append(self._event("setpoint", ch.describe(units), f"error: {e}", ch))
                continue
            except SliceError as e:
                # A read that failed; the next step tries again. A slice that
                # goes unreachable aborts the profile through its event.
                out.append(self._event("setpoint", ch.describe(units), f"error: {e}", ch))
                continue
            ch.sent = units
            out.append(self._event("setpoint", ch.describe(units), channel=ch))
        return out

    def _finish(self) -> list[Result | Event]:
        out = self._make_safe()
        out.append(self._event("profile-end", f"{self.profile.name!r} finished"))
        self._close("completed", "the profile finished")
        return out

    def abort(self, why: str) -> list[Result | Event]:
        """Stop the profile and send every channel it touched its safe state.

        Call it on the controller's thread.
        """
        if self.done:
            return []
        out = self._make_safe()
        out.append(self._event("profile-abort", why, result="aborted"))
        self._close("stopped", why)
        return out

    def _make_safe(self) -> list[Result | Event]:
        """Send 0 to each channel the profile sent anything to.

        A channel that will not take it is set to 0 as wanted without
        sending, so its slice's own supervision stops it: the next poll
        finds it running another value than wanted, and the check that
        follows sends 0 or the safe state. Nothing sends the profile's
        value again.
        """
        out: list[Result | Event] = []
        for ch in self._channels.values():
            if ch.sent is None:
                continue
            try:
                if ch.wanted() != 0:
                    ch.send(0)
                result = "ok"
            except (SliceBusy, SliceError, CrumbsError, OSError) as e:
                ch.forget()
                result = f"error: {e}; set to 0 as wanted, for the slice's checks to send"
            out.append(self._event("setpoint", ch.describe(0), result, ch))
        return out

    def _close(self, status: str, why: str) -> None:
        self.done = True
        self._finished(status, why)
