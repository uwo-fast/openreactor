"""Run a profile: step each channel's setpoint on the controller's thread.

The runner is polled on every controller tick, and at each slice poll
period it works out what each channel in the profile should run and sends
what changed. When the profile ends, or is aborted, every channel it
touched goes to its safe state.

A profile never overrides anything else: if a channel no longer runs what
the profile last sent it, because of stop-all, an e-stop, a reboot, a
drift the slice was stopped for, or a setpoint from the operator, the
profile is aborted. So a profile cannot restart a heater or a motor that
something else stopped.
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

    def advance(self, now: float) -> list[Result | Event]:
        if self.done:
            return []
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
                return out + self.abort(f"{name}: {e}")
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
        """Send 0 to each channel the profile sent anything to."""
        out: list[Result | Event] = []
        for ch in self._channels.values():
            if ch.sent is None:
                continue
            try:
                if ch.wanted() != 0:
                    ch.send(0)
                result = "ok"
            except (SliceBusy, SliceError, CrumbsError, OSError) as e:
                # A slice that will not take it is stopped by its own
                # supervision: a check that finds it changed sends its
                # safe state, and its watchdog trips if it is lost.
                result = f"error: {e}"
            out.append(self._event("setpoint", ch.describe(0), result, ch))
        return out

    def _close(self, status: str, why: str) -> None:
        self.done = True
        self._finished(status, why)
