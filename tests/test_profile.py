from pathlib import Path

import pytest

from openreactor import cli
from openreactor.profile import (
    OFF,
    ProfileError,
    clock,
    load_profile,
    parse_profile,
    profile_from_bytes,
    timeline,
)

EXAMPLES = Path(__file__).parents[1] / "examples"
EXAMPLE = EXAMPLES / "profiles" / "ramp-and-hold.toml"


def profile(**channels):
    return parse_profile({"version": 1, "name": "p", "channels": channels})


def problems(data, channels=None) -> list[str]:
    with pytest.raises(ProfileError) as e:
        parse_profile(data, channels)
    return [str(p) for p in e.value.problems]


def bad_steps(*steps) -> list[str]:
    return problems({"version": 1, "name": "p", "channels": {"jacket": list(steps)}})


def bad_step(*steps) -> str:
    """The one problem with ``steps``, after a set and a hold that are fine."""
    [message] = bad_steps({"set": 1}, {"hold": "1m"}, *steps)
    return message


def test_the_example_is_valid_against_the_example_config():
    from openreactor.config import load_config

    config = load_config(EXAMPLES / "openreactor.toml")
    channels = {ch.name for d in config.devices for ch in d.channels}
    p = load_profile(EXAMPLE, channels)
    assert p.name == "Ramp and hold"
    assert list(p.channels) == ["jacket", "stirrer"]
    assert p.duration == 2.5 * 3600


def test_set_ramp_hold_and_off_give_the_setpoint_at_each_moment():
    p = profile(jacket=[{"set": 30}, {"ramp": 37, "over": "30m"}, {"hold": "2h"}, {"off": True}])
    at = lambda t: p.value_at("jacket", t)  # noqa: E731
    assert at(0) == 30.0
    assert at(15 * 60) == pytest.approx(33.5)
    assert at(30 * 60) == 37.0
    assert at(2 * 3600) == 37.0
    assert at(2.5 * 3600) == OFF
    assert at(10 * 3600) == OFF
    assert p.duration == 2.5 * 3600


def test_a_ramp_from_an_explicit_value():
    p = profile(jacket=[{"ramp": 40, "from": 20, "over": "10s"}])
    assert p.value_at("jacket", 0) == 20.0
    assert p.value_at("jacket", 5) == 30.0
    assert p.value_at("jacket", 9.999) == pytest.approx(40.0, abs=0.01)
    # From the profile's end on, every channel is off, never beyond its ramp.
    assert p.value_at("jacket", 10) == OFF
    assert p.value_at("jacket", 60) == OFF


def test_a_ramp_down_and_a_from_that_jumps():
    p = profile(jacket=[{"set": 50}, {"hold": "10s"}, {"ramp": 10, "from": 30, "over": "20s"}])
    assert p.value_at("jacket", 9.9) == 50.0
    assert p.value_at("jacket", 10) == 30.0
    assert p.value_at("jacket", 20) == 20.0


def test_a_leading_hold_waits_with_the_channel_untouched():
    p = profile(stirrer=[{"hold": "1m"}, {"set": 100}, {"hold": "1m"}])
    assert p.value_at("stirrer", 30) is None
    assert p.value_at("stirrer", 60) == 100.0


def test_channels_run_side_by_side_and_all_go_off_at_the_end():
    p = profile(jacket=[{"set": 30}, {"hold": "1h"}], stirrer=[{"set": 50}, {"hold": "2h"}])
    assert p.duration == 2 * 3600
    # The jacket's steps are done, but the profile is not: it keeps its value.
    assert p.value_at("jacket", 1.5 * 3600) == 30.0
    assert p.value_at("jacket", 2 * 3600) == OFF
    assert p.value_at("stirrer", 2 * 3600) == OFF


def test_each_segment_knows_its_step():
    p = profile(jacket=[{"set": 30}, {"ramp": 37, "over": "1m"}, {"hold": "1m"}, {"off": True}])
    assert [(s.step, s.action) for s in p.channels["jacket"]] == [
        (0, "set"),
        (1, "ramp"),
        (2, "hold"),
        (3, "off"),
    ]


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30s", 30), ("2m", 120), ("1.5h", 5400), ("0.5s", 0.5), (" 10m ", 600), ("720h", 2592000)],
)
def test_durations(text: str, seconds: float):
    assert profile(jacket=[{"set": 1}, {"hold": text}]).duration == seconds


@pytest.mark.parametrize(
    "text", ["2h15m", "-1m", "0s", "0.0009s", "1d", "m", "1.m", "1e3s", "", "٣٠s"]
)
def test_bad_durations(text: str):
    message = bad_step({"hold": text})
    assert message.startswith("channels.jacket[2].hold: ")
    assert "s, m or h" in message


def test_a_step_longer_than_30_days_is_rejected():
    for text in ("721h", "1000000000000000h", "9" * 400 + "h"):
        assert bad_step({"hold": text}) == (
            "channels.jacket[2].hold: must be at most 30 days (split a longer step into several)"
        ), text[:10]


def test_a_first_step_ramp_without_from_is_rejected():
    [message] = bad_steps({"ramp": 37, "over": "30m"})
    assert message == (
        "channels.jacket[0]: a ramp as the first step has no value to start from "
        "(add from = <value>, or a set step before it)"
    )


def test_a_ramp_after_off_without_from_is_rejected():
    message = bad_step({"off": True}, {"ramp": 37, "over": "1m"})
    assert message.startswith("channels.jacket[3]: a ramp after off has no value to start from")


def test_a_ramp_after_a_leading_hold_without_from_is_rejected():
    [message] = bad_steps({"hold": "1m"}, {"ramp": 37, "over": "1m"})
    assert message.startswith("channels.jacket[1]: a ramp before any set has no value")


def test_a_ramp_after_a_step_that_failed_is_not_reported_twice():
    assert bad_step({"set": "x"}, {"ramp": 3, "over": "1m"}).startswith(
        "channels.jacket[2].set: must be a number"
    )
    assert bad_step({"ramp": "x", "over": "1m"}, {"ramp": 3, "over": "1m"}).startswith(
        "channels.jacket[2].ramp: must be a number"
    )


def test_a_hold_or_a_ramp_without_a_duration_is_rejected():
    assert bad_step({"hold": True}) == (
        "channels.jacket[2].hold: must be a duration string (a number and s, m or h, "
        'such as "30s", "2m" or "1.5h")'
    )
    assert bad_step({"ramp": 5}) == (
        'channels.jacket[2].over: is required (a ramp takes time: add over = "30m")'
    )


def test_each_step_has_one_action_and_only_its_own_keys():
    one = "must have exactly one of set, ramp, hold and off (for example { set = 30.0 })"
    assert bad_step({"set": 1, "off": True}) == f"channels.jacket[2]: {one}"
    assert bad_step({"sett": 1}) == f"channels.jacket[2]: {one}"
    assert bad_step({"set": 1, "over": "1m"}) == (
        "channels.jacket[2].over: is not part of a set step (a set step takes set)"
    )
    assert bad_step({"off": True, "from": 1}) == (
        "channels.jacket[2].from: is not part of an off step (an off step takes off)"
    )
    assert bad_step({"off": "yes"}) == (
        "channels.jacket[2].off: must be true (write { off = true })"
    )
    assert bad_step(30) == "channels.jacket[2]: must be a table (for example { set = 30.0 })"


def test_values_must_be_finite_numbers():
    number = "(write a plain number such as 37.0)"
    assert bad_step({"set": "hot"}) == f"channels.jacket[2].set: must be a number {number}"
    assert bad_step({"set": True}) == f"channels.jacket[2].set: must be a number {number}"
    for value in (float("inf"), 10**400):
        assert bad_step({"set": value}) == (
            f"channels.jacket[2].set: must be a finite number {number}"
        )
    assert bad_step({"ramp": float("nan"), "from": 1, "over": "1m"}) == (
        f"channels.jacket[2].ramp: must be a finite number {number}"
    )
    assert bad_step({"ramp": 1, "from": -(10**400), "over": "1m"}) == (
        f"channels.jacket[2].from: must be a finite number {number}"
    )


def test_a_huge_integer_from_toml_is_a_problem_not_a_crash():
    text = (
        f'version = 1\nname = "p"\n[channels]\nj = [{{ set = 1{"0" * 400} }}, {{ hold = "1m" }}]\n'
    )
    with pytest.raises(ProfileError, match="must be a finite number"):
        profile_from_bytes(text.encode())


def test_a_channel_that_never_sets_a_value_is_rejected():
    assert problems({"version": 1, "name": "p", "channels": {"j": [{"hold": "1h"}]}}) == [
        "channels.j: never sets a value (add a set, a ramp or an off step)"
    ]


def test_a_profile_that_takes_no_time_is_rejected():
    assert problems({"version": 1, "name": "p", "channels": {"j": [{"set": 30}]}}) == [
        "channels: the profile takes no time (add a hold or a ramp step)"
    ]


@pytest.mark.parametrize("name", ["", "Jacket", "a.b", "1st", "fan-1"])
def test_channel_names_follow_the_config_rule(name: str):
    data = {"version": 1, "name": "p", "channels": {name: [{"set": 1}, {"hold": "1m"}]}}
    assert problems(data) == [
        f"channels.{name}: is not a channel name (channel names use a-z, 0-9 and _)"
    ]


@pytest.mark.parametrize("data", [[], "x", None, 5])
def test_a_body_that_is_not_a_table_is_a_problem_not_a_crash(data):
    assert problems(data) == ["profile: must be a table (start with version = 1)"]


def test_every_problem_is_reported_in_one_pass():
    found = problems(
        {
            "name": "",
            "colour": "red",
            "channels": {"jacket": [{"ramp": 1, "over": "1m"}, {"hold": "1d"}], "fan": []},
        },
        channels={"jacket", "stirrer"},
    )
    assert found == [
        "colour: unknown key (expected version, name, notes and channels)",
        "version: is required (add version = 1)",
        'name: must be a non-empty string (for example name = "Ramp and hold")',
        "channels.jacket[0]: a ramp as the first step has no value to start from "
        "(add from = <value>, or a set step before it)",
        "channels.jacket[1].hold: '1d' is not a duration "
        '(a number and s, m or h, such as "30s", "2m" or "1.5h")',
        "channels.fan: is not an actuated channel in the config (expected one of jacket, stirrer)",
        "channels.fan: must be a non-empty list of steps (for example [{ set = 30.0 }])",
    ]


def test_version_must_be_the_integer_1():
    steps = {"j": [{"set": 1}, {"hold": "1m"}]}
    for version in (2, "1", True, 1.5, 1.0):
        assert problems({"version": version, "name": "p", "channels": steps}) == [
            "version: must be 1 (this openreactor reads version 1 profiles)"
        ], version


def test_channels_are_required():
    assert problems({"version": 1, "name": "p"}) == [
        "channels: is required (add a [channels] table with a list of steps per channel)"
    ]
    assert problems({"version": 1, "name": "p", "channels": {}}) == [
        "channels: must be a table with at least one channel "
        "(for example jacket = [{ set = 30.0 }] under [channels])"
    ]


def test_uploaded_text_that_is_not_toml_or_utf8():
    with pytest.raises(ProfileError, match="upload: not valid TOML"):
        profile_from_bytes(b"version = = 1", source="upload")
    with pytest.raises(ProfileError, match="upload: not valid UTF-8"):
        profile_from_bytes(b"name = '\xff'", source="upload")


def test_clock():
    assert clock(0) == "0:00:00"
    assert clock(5400) == "1:30:00"
    assert clock(61.5) == "0:01:01.5"
    assert clock(0.01) == "0:00:00.01"
    assert clock(0.0004) == "0:00:00"
    assert clock(36 * 3600) == "36:00:00"


def test_the_timeline_of_the_example():
    assert timeline(load_profile(EXAMPLE)) == [
        "  0:00:00  jacket   set 30",
        "  0:00:00  jacket   ramp 30 to 37 over 0:30:00",
        "  0:00:00  stirrer  set 120",
        "  0:00:00  stirrer  hold 120 for 0:10:00",
        "  0:10:00  stirrer  ramp 120 to 300 over 0:05:00",
        "  0:15:00  stirrer  hold 300 for 2:15:00",
        "  0:30:00  jacket   hold 37 for 2:00:00",
        "  2:30:00  jacket   off",
        "  2:30:00  end: jacket, stirrer go safe",
    ]


# openreactor profile validate


def test_validate_prints_a_summary(capsys):
    assert cli.main(["profile", "validate", str(EXAMPLE)]) == 0
    assert capsys.readouterr().out == f"{EXAMPLE}: OK, 'Ramp and hold', 2 channel(s), 2:30:00\n"


def test_validate_dry_run_prints_the_timeline(capsys):
    config = str(EXAMPLES / "openreactor.toml")
    assert cli.main(["profile", "validate", "--dry-run", "-c", config, str(EXAMPLE)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "  0:00:00  jacket   set 30"
    assert out[-1] == "  2:30:00  end: jacket, stirrer go safe"


def test_validate_checks_channels_against_a_config(tmp_path: Path, capsys):
    path = tmp_path / "p.toml"
    path.write_text('version = 1\nname = "p"\n[channels]\nph = [{ set = 7 }, { hold = "1m" }]\n')
    assert cli.main(["profile", "validate", str(path)]) == 0
    capsys.readouterr()
    config = str(EXAMPLES / "openreactor.toml")
    assert cli.main(["profile", "validate", "-c", config, str(path)]) == 1
    assert capsys.readouterr().err == (
        f"{path}: channels.ph: is not an actuated channel in the config "
        "(expected one of jacket, stirrer)\n"
    )


def test_validate_reports_a_missing_file(tmp_path: Path, capsys):
    assert cli.main(["profile", "validate", str(tmp_path / "nope.toml")]) == 1
    assert "No such file or directory" in capsys.readouterr().err


def test_a_channel_kept_off_for_the_run_is_valid():
    p = profile(jacket=[{"set": 30}, {"hold": "1h"}], stirrer=[{"off": True}])
    assert p.value_at("stirrer", 0) == OFF


def test_a_set_at_the_profiles_end_is_rejected():
    assert bad_steps({"set": 1}, {"hold": "1h"}, {"set": 5}) == [
        "channels.jacket[2]: comes at the profile's end, when every channel goes off "
        "(add a hold after it, or remove it)"
    ]
    # An off there is what happens anyway, and is fine.
    profile(jacket=[{"set": 1}, {"hold": "1h"}, {"off": True}])


def test_value_at_an_unknown_channel_or_nan():
    p = profile(jacket=[{"set": 1}, {"hold": "1h"}])
    for t in (0, 7200):
        with pytest.raises(KeyError):
            p.value_at("nothing", t)
    assert p.value_at("jacket", float("nan")) == OFF
