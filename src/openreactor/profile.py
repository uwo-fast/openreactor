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
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(s|m|h)")
_UNIT_S = {"s": 1.0, "m": 60.0, "h": 3600.0}

# What a channel is told at a moment: a value, its safe state, or nothing
# yet (the profile has not reached its first set or ramp).
Setpoint = float | Literal["off"] | None
OFF: Literal["off"] = "off"


@dataclass(frozen=True)
class Segment:
    """One step, placed in time. ``set`` and ``off`` take no time."""

    action: str
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
        """The setpoint for ``channel`` at ``t`` seconds from the start."""
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


def parse_profile(data: dict[str, Any], channels: Collection[str] | None = None) -> Profile:
    problems: list[Problem] = []

    def error(path: str, message: str, hint: str = "") -> None:
        problems.append(Problem(path, message, hint))

    for key in data:
        if key not in ("version", "name", "notes", "channels"):
            error(key, "unknown key", "expected version, name, notes and channels")

    version = data.get("version")
    if version is None:
        error("version", "is required", f"add version = {VERSION}")
    elif isinstance(version, bool) or version != VERSION:
        error("version", f"must be {VERSION}", "this openreactor reads version 1 profiles")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        error("name", "must be a non-empty string", 'for example name = "Ramp and hold"')
        name = ""
    notes = data.get("notes", "")
    if not isinstance(notes, str):
        error("notes", "must be a string")
        notes = ""

    raw = data.get("channels")
    timelines: dict[str, tuple[Segment, ...]] = {}
    if raw is None:
        error("channels", "is required", "add a [channels] table with a list of steps per channel")
    elif not isinstance(raw, dict) or not raw:
        error("channels", "must be a table with at least one channel")
    else:
        for channel, steps in raw.items():
            path = f"channels.{channel}"
            if channels is not None and channel not in channels:
                known = ", ".join(sorted(channels)) or "none"
                error(path, "is not an actuated channel in the config", f"expected one of {known}")
            segments = _steps(steps, path, error)
            if segments:
                timelines[channel] = segments

    if problems:
        raise ProfileError(problems)
    return Profile(name.strip(), notes, timelines)


def _steps(steps: Any, path: str, error: Any) -> tuple[Segment, ...]:
    if not isinstance(steps, list) or not steps:
        error(path, "must be a non-empty list of steps", "for example [{ set = 30.0 }]")
        return ()
    segments: list[Segment] = []
    t = 0.0
    value: Setpoint = None
    for i, step in enumerate(steps):
        spath = f"{path}[{i}]"
        if not isinstance(step, dict):
            error(spath, "must be a table such as { set = 30.0 }")
            continue
        actions = [a for a in ACTIONS if a in step]
        if len(actions) != 1:
            error(spath, "must have exactly one of set, ramp, hold and off")
            continue
        action = actions[0]
        allowed = {"set": ("set",), "ramp": ("ramp", "over", "from"), "hold": ("hold",)}
        for key in step:
            if key not in allowed.get(action, ("off",)):
                article = "an" if action == "off" else "a"
                error(f"{spath}.{key}", f"is not part of {article} {action} step")

        if action == "set":
            target = _number(step["set"], f"{spath}.set", error)
            if target is not None:
                segments.append(Segment("set", t, t, value, target))
                value = target
        elif action == "off":
            if step["off"] is not True:
                error(f"{spath}.off", "must be true", "write { off = true }")
            segments.append(Segment("off", t, t, value, OFF))
            value = OFF
        elif action == "hold":
            span = _duration(step["hold"], f"{spath}.hold", error)
            if span is not None:
                segments.append(Segment("hold", t, t + span, value, value))
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
            elif not isinstance(value, float):
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
                segments.append(Segment("ramp", t, t + span, start, target))
            if span is not None:
                t += span
            value = target if target is not None else value
    return tuple(segments)


def _number(value: Any, path: str, error: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        error(path, "must be a number")
        return None
    if not math.isfinite(value):
        error(path, "must be a finite number")
        return None
    return float(value)


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
    if not math.isfinite(seconds) or seconds <= 0:
        error(path, "must be longer than zero", hint)
        return None
    return seconds


def clock(seconds: float) -> str:
    """``H:MM:SS`` from the start, with tenths when they are not zero."""
    tenths = round(seconds * 10)
    h, rest = divmod(tenths, 36000)
    m, rest = divmod(rest, 600)
    s, tenth = divmod(rest, 10)
    return f"{h}:{m:02d}:{s:02d}" + (f".{tenth}" if tenth else "")


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
