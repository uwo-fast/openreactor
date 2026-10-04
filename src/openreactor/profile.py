"""Run profiles: a schedule of setpoints for each actuated channel.

A profile is a TOML file::

    version = 1
    name = "Ramp and hold"

    [channels]
    jacket = [
      { set = 30.0 },
      { ramp = 37.0, over = "30m" },
      { hold = "2h" },
      { off = true },
    ]

Each channel's steps run in order from the profile's start:

- ``set``: the value, at once.
- ``ramp``: to the value, in a straight line ``over`` a duration, from the
  value before it or from an explicit ``from``.
- ``hold``: keep what is there for a duration.
- ``off``: the channel's safe state, at once.

Durations are a number and a unit: ``30s``, ``2m``, ``1.5h``. The same
validator serves the CLI, the API and the UI; every problem names its place
in the file and how to fix it.
"""

from __future__ import annotations

import bisect
import math
import re
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

VERSION = 1
ACTIONS = ("set", "ramp", "hold", "off")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(s|m|h)", re.ASCII)
_NAME = re.compile(r"[a-z][a-z0-9_]*")  # as for channels in the config
MAX_STEP_S = 30 * 24 * 3600.0
_FINITE_HINT = "write a plain number such as 37.0"
_UNIT_S = {"s": 1.0, "m": 60.0, "h": 3600.0}

# What a channel is told at a moment: a value, its safe state, or nothing
# yet (the profile has not reached its first set or ramp).
Setpoint = float | Literal["off"] | None
OFF: Literal["off"] = "off"


@dataclass(frozen=True)
class Segment:
    """One step, placed in time. ``set`` and ``off`` take no time."""

    action: str
    step: int  # its index in the channel's list of steps
    start: float  # seconds from the profile's start
    end: float
    start_value: Setpoint
    end_value: Setpoint

    def value_at(self, t: float) -> Setpoint:
        if self.action != "ramp" or t >= self.end:
            return self.end_value
        a, b = self.start_value, self.end_value
        assert isinstance(a, float) and isinstance(b, float)
        return a + (b - a) * (t - self.start) / (self.end - self.start)


@dataclass(frozen=True)
class Profile:
    name: str
    notes: str
    channels: dict[str, tuple[Segment, ...]]

    @property
    def duration(self) -> float:
        return max((s[-1].end for s in self.channels.values()), default=0.0)

    def value_at(self, channel: str, t: float) -> Setpoint:
        """The setpoint for ``channel`` at ``t`` seconds from the start.
        From the profile's end on, every channel is off."""
        if t >= self.duration:
            return OFF
        segments = self.channels[channel]
        i = bisect.bisect_right([s.start for s in segments], t) - 1
        return None if i < 0 else segments[i].value_at(t)


@dataclass(frozen=True)
class Problem:
    path: str
    message: str
    hint: str = ""

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" + (f" ({self.hint})" if self.hint else "")


class ProfileError(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("\n".join(str(p) for p in problems))


def load_profile(path: str | Path, channels: Collection[str] | None = None) -> Profile:
    """Read and validate a profile file. ``channels``, if given, are the
    actuated channels it may use. Raises ProfileError listing every problem."""
    with open(path, "rb") as f:
        return profile_from_bytes(f.read(), channels, str(path))


def profile_from_bytes(
    data: bytes, channels: Collection[str] | None = None, source: str = "profile"
) -> Profile:
    """Validate a profile's text, as uploaded or read from a file."""
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as e:
        raise ProfileError([Problem(source, f"not valid UTF-8: {e.reason}")]) from e
    except tomllib.TOMLDecodeError as e:
        raise ProfileError([Problem(source, f"not valid TOML: {e}")]) from e
    return parse_profile(parsed, channels)


def parse_profile(data: Any, channels: Collection[str] | None = None) -> Profile:
    """Validate a parsed profile: from TOML, or a JSON body."""
    if not isinstance(data, dict):
        raise ProfileError([Problem("profile", "must be a table", "start with version = 1")])
    problems: list[Problem] = []

    def error(path: str, message: str, hint: str = "") -> None:
        problems.append(Problem(path, message, hint))

    for key in data:
        if key not in ("version", "name", "notes", "channels"):
            error(key, "unknown key", "expected version, name, notes and channels")

    version = data.get("version")
    if version is None:
        error("version", "is required", f"add version = {VERSION}")
    elif type(version) is not int or version != VERSION:
        error("version", f"must be {VERSION}", "this openreactor reads version 1 profiles")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        error("name", "must be a non-empty string", 'for example name = "Ramp and hold"')
        name = ""
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        error("notes", "must be a string", 'for example notes = "batch 4"')
        notes = ""

    raw = data.get("channels")
    timelines: dict[str, tuple[Segment, ...]] = {}
    if raw is None:
        error("channels", "is required", "add a [channels] table with a list of steps per channel")
    elif not isinstance(raw, dict) or not raw:
        error(
            "channels",
            "must be a table with at least one channel",
            "for example jacket = [{ set = 30.0 }] under [channels]",
        )
    else:
        for channel, steps in raw.items():
            path = f"channels.{channel}"
            if not _NAME.fullmatch(channel):
                error(path, "is not a channel name", "channel names use a-z, 0-9 and _")
                continue
            if channels is not None and channel not in channels:
                known = ", ".join(sorted(channels)) or "none"
                error(path, "is not an actuated channel in the config", f"expected one of {known}")
            before = len(problems)
            segments = _steps(steps, path, error)
            if segments is None:
                continue
            # Only for steps that are otherwise right: a ramp that failed is
            # not also a channel that never sets a value.
            if len(problems) == before and not any(s.action in ("set", "ramp") for s in segments):
                error(path, "never sets a value", "add a set or a ramp step")
            timelines[channel] = segments

    profile = Profile(name.strip(), notes, timelines)
    if not problems and profile.duration == 0:
        error("channels", "the profile takes no time", "add a hold or a ramp step")
    if problems:
        raise ProfileError(problems)
    return profile


def _steps(steps: Any, path: str, error: Any) -> tuple[Segment, ...] | None:
    if not isinstance(steps, list) or not steps:
        error(path, "must be a non-empty list of steps", "for example [{ set = 30.0 }]")
        return None
    segments: list[Segment] = []
    t = 0.0
    value: Setpoint = None
    # After a step that failed, the value is unknown: a ramp without from
    # is then not reported a second time.
    known = True
    for i, step in enumerate(steps):
        spath = f"{path}[{i}]"
        if not isinstance(step, dict):
            error(spath, "must be a table", "for example { set = 30.0 }")
            known = False
            continue
        actions = [a for a in ACTIONS if a in step]
        if len(actions) != 1:
            error(
                spath,
                "must have exactly one of set, ramp, hold and off",
                "for example { set = 30.0 }",
            )
            known = False
            continue
        action = actions[0]
        allowed = {"set": ("set",), "ramp": ("ramp", "over", "from"), "hold": ("hold",)}
        for key in step:
            if key not in allowed.get(action, ("off",)):
                article = "an" if action == "off" else "a"
                keys = " and ".join(allowed.get(action, ("off",)))
                error(
                    f"{spath}.{key}",
                    f"is not part of {article} {action} step",
                    f"{article} {action} step takes {keys}",
                )

        if action == "set":
            target = _number(step["set"], f"{spath}.set", error)
            if target is not None:
                segments.append(Segment("set", i, t, t, value, target))
                value, known = target, True
            else:
                known = False
        elif action == "off":
            if step["off"] is not True:
                error(f"{spath}.off", "must be true", "write { off = true }")
            segments.append(Segment("off", i, t, t, value, OFF))
            value, known = OFF, True
        elif action == "hold":
            span = _duration(step["hold"], f"{spath}.hold", error)
            if span is not None:
                segments.append(Segment("hold", i, t, t + span, value, value))
                t += span
        else:
            target = _number(step["ramp"], f"{spath}.ramp", error)
            if "over" not in step:
                error(f"{spath}.over", "is required", 'a ramp takes time: add over = "30m"')
                span = None
            else:
                span = _duration(step["over"], f"{spath}.over", error)
            start: Setpoint = value
            if "from" in step:
                start = _number(step["from"], f"{spath}.from", error)
            elif known and not isinstance(value, float):
                if i == 0:
                    where = "as the first step"
                elif value == OFF:
                    where = "after off"
                else:
                    where = "before any set"
                error(
                    spath,
                    f"a ramp {where} has no value to start from",
                    "add from = <value>, or a set step before it",
                )
            if target is not None and span is not None and isinstance(start, float):
                segments.append(Segment("ramp", i, t, t + span, start, target))
            if span is not None:
                t += span
            value = target
            known = target is not None
    return tuple(segments)


def _number(value: Any, path: str, error: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        error(path, "must be a number", _FINITE_HINT)
        return None
    try:
        number = float(value)  # an integer of any size parses from TOML
    except OverflowError:
        number = math.inf
    if not math.isfinite(number):
        error(path, "must be a finite number", _FINITE_HINT)
        return None
    return number


def _duration(value: Any, path: str, error: Any) -> float | None:
    hint = 'a number and s, m or h, such as "30s", "2m" or "1.5h"'
    if not isinstance(value, str):
        error(path, "must be a duration string", hint)
        return None
    match = _DURATION.fullmatch(value.strip())
    if match is None:
        error(path, f"{value!r} is not a duration", hint)
        return None
    seconds = float(match.group(1)) * _UNIT_S[match.group(2)]
    if seconds <= 0:
        error(path, "must be longer than zero", hint)
        return None
    if seconds > MAX_STEP_S:
        error(path, "must be at most 30 days", "split a longer step into several")
        return None
    return seconds


def clock(seconds: float) -> str:
    """``H:MM:SS`` from the start, with milliseconds when they are not zero."""
    ms = round(seconds * 1000)
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, frac = divmod(rest, 1000)
    return f"{h}:{m:02d}:{s:02d}" + (f".{frac:03d}".rstrip("0") if frac else "")


def _value(v: Setpoint) -> str:
    if v is None:
        return "nothing"
    return v if isinstance(v, str) else f"{v:g}"


def timeline(profile: Profile) -> list[str]:
    """What happens when, one line per step, in time order."""
    rows: list[tuple[float, int, int, str]] = []
    width = max(len(c) for c in profile.channels)
    for order, (channel, segments) in enumerate(profile.channels.items()):
        for i, s in enumerate(segments):
            if s.action == "set":
                what = f"set {_value(s.end_value)}"
            elif s.action == "off":
                what = "off"
            elif s.action == "hold":
                what = f"hold {_value(s.start_value)} for {clock(s.end - s.start)}"
            else:
                what = (
                    f"ramp {_value(s.start_value)} to {_value(s.end_value)} "
                    f"over {clock(s.end - s.start)}"
                )
            rows.append((s.start, order, i, f"{clock(s.start):>9}  {channel:<{width}}  {what}"))
    lines = [row[3] for row in sorted(rows)]
    touched = ", ".join(profile.channels)
    lines.append(f"{clock(profile.duration):>9}  end: {touched} go safe")
    return lines
