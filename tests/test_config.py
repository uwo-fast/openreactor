import copy
from pathlib import Path
from typing import Any

import pytest

from openreactor.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).parents[1] / "examples" / "openreactor.toml"

BASE: dict[str, Any] = {
    "controller": {"slice_poll_s": 1.0, "ezo_period_s": 2.0, "watchdog_timeout_ms": 5000},
    "device": [
        {
            "name": "heater",
            "kind": "rlht",
            "bus": "/dev/i2c-1",
            "address": 0x0A,
            "channels": {"jacket": {"output": 1, "tc": 1}},
        },
        {
            "name": "motors",
            "kind": "dcmt",
            "bus": "/dev/i2c-1",
            "address": 0x0B,
            "channels": {"stirrer": {"motor": 1}},
        },
        {"name": "rtd", "kind": "ezo-rtd", "bus": "/dev/i2c-1", "address": 0x66},
        {"name": "ph", "kind": "ezo-ph", "bus": "/dev/i2c-1", "address": 0x63, "temp_comp": "rtd"},
    ],
}


def config(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    data.update(changes)
    return data


def problems(data: dict[str, Any]) -> list[str]:
    with pytest.raises(ConfigError) as e:
        parse_config(data)
    return [str(p) for p in e.value.problems]


def test_base_config_is_valid():
    parsed = parse_config(config())
    assert [d.name for d in parsed.devices] == ["heater", "motors", "rtd", "ph"]
    assert parsed.devices[0].channels[0].label == "jacket"


def test_example_config_is_valid():
    parsed = load_config(EXAMPLE)
    assert {d.kind for d in parsed.devices} >= {"rlht", "dcmt", "ezo-rtd", "ezo-ph"}


def test_empty_config_uses_defaults():
    parsed = parse_config({})
    assert parsed.server.host == "127.0.0.1"
    assert parsed.controller.watchdog_timeout_ms == 5000
    assert parsed.devices == ()


# One test per validation rule.


def test_rejects_duplicate_bus_and_address():
    data = config()
    data["device"][3]["address"] = 0x66
    assert problems(data) == ["device[3].address: 0x66 on /dev/i2c-1 is already used by 'rtd'"]


def test_same_address_on_another_bus_is_allowed():
    data = config()
    data["device"][3]["address"] = 0x66
    data["device"][3]["bus"] = "/dev/i2c-3"
    parse_config(data)


def test_rejects_unknown_kind():
    data = config()
    data["device"][2]["kind"] = "ezo-flow"
    assert problems(data)[0].startswith("device[2].kind: unknown kind 'ezo-flow'")


@pytest.mark.parametrize("address", [0x07, 0x78, 0x00, 0x7F])
def test_rejects_address_outside_range(address: int):
    data = config()
    data["device"][2]["address"] = address
    assert problems(data) == [
        f"device[2].address: 0x{address:02X} is outside the device range 0x08 to 0x77"
    ]


@pytest.mark.parametrize("address", [0x08, 0x77])
def test_accepts_address_range_ends(address: int):
    data = config()
    data["device"][2]["address"] = address
    parse_config(data)


def test_rejects_duplicate_channel_name_across_slices():
    data = config()
    data["device"][1]["channels"] = {"jacket": {"motor": 1}}
    assert problems(data) == [
        "device[1].channels.jacket: name 'jacket' is already used by channel 'jacket' of 'heater'"
    ]


def test_rejects_ezo_device_named_like_a_slice_channel():
    data = config()
    data["device"][2]["name"] = "stirrer"
    data["device"][3]["temp_comp"] = "stirrer"
    assert problems(data) == [
        "device[2].name: name 'stirrer' is already used by channel 'stirrer' of 'motors'"
    ]


def test_rejects_duplicate_device_name():
    data = config()
    data["device"][3]["name"] = "rtd"
    data["device"][3]["kind"] = "ezo-orp"
    del data["device"][3]["temp_comp"]
    assert problems(data) == ["device[3].name: name 'rtd' is already used by device 'rtd'"]


def test_rejects_two_channels_on_one_output():
    data = config()
    data["device"][0]["channels"]["jacket2"] = {"output": 1, "tc": 2}
    assert problems(data) == [
        "device[0].channels.jacket2.output: output 1 is already used by 'jacket'"
    ]


def test_rejects_temp_comp_pointing_at_a_non_rtd():
    data = config()
    data["device"][3]["temp_comp"] = "heater"
    assert problems(data) == ["device[3].temp_comp: must name an ezo-rtd device; 'heater' is rlht"]


def test_rejects_temp_comp_pointing_at_nothing():
    data = config()
    data["device"][3]["temp_comp"] = "missing"
    assert problems(data) == ["device[3].temp_comp: 'missing' is not a device"]


def test_rejects_temp_comp_on_a_kind_without_it():
    data = config()
    data["device"][2]["temp_comp"] = "rtd"
    assert problems(data) == ["device[2].temp_comp: unknown key"]


def test_rejects_watchdog_below_three_polls():
    data = config(controller={"slice_poll_s": 1.0, "watchdog_timeout_ms": 2999})
    assert problems(data) == [
        "controller.watchdog_timeout_ms: must be between 3 x slice_poll_s (3000 ms) and 65535 ms"
    ]


def test_accepts_watchdog_at_exactly_three_polls():
    parse_config(config(controller={"slice_poll_s": 0.1, "watchdog_timeout_ms": 300}))
    parse_config(config(controller={"slice_poll_s": 1.0, "watchdog_timeout_ms": 3000}))


def test_rejects_watchdog_above_u16():
    data = config(controller={"watchdog_timeout_ms": 65536})
    assert problems(data) == [
        "controller.watchdog_timeout_ms: must be between 3 x slice_poll_s (3000 ms) and 65535 ms"
    ]


def test_accepts_watchdog_at_u16_max():
    parse_config(config(controller={"watchdog_timeout_ms": 65535}))


def test_rejects_ezo_period_below_slowest_read_plus_margin():
    # pH with temperature compensation reads in 900 ms, so 1.1 s is the minimum.
    data = config(controller={"ezo_period_s": 1.0})
    assert problems(data) == [
        "controller.ezo_period_s: must be at least 1.1 s: "
        "a read from 'ph' takes 900 ms, plus 200 ms"
    ]


def test_accepts_ezo_period_at_exactly_the_minimum():
    parse_config(config(controller={"ezo_period_s": 1.1}))


def test_ezo_period_uses_the_slowest_family():
    data = config(controller={"ezo_period_s": 1.1})
    data["device"].append({"name": "orp", "kind": "ezo-orp", "bus": "/dev/i2c-1", "address": 0x62})
    assert problems(data) == [
        "controller.ezo_period_s: must be at least 1.2 s: "
        "a read from 'orp' takes 1000 ms, plus 200 ms"
    ]


def test_ezo_period_uses_the_temperature_compensated_delay():
    # EC reads in 600 ms, or 900 ms with temperature compensation.
    data = config(controller={"ezo_period_s": 1.0})
    data["device"][3] = {"name": "ec", "kind": "ezo-ec", "bus": "/dev/i2c-1", "address": 0x64}
    parse_config(data)
    data["device"][3]["temp_comp"] = "rtd"
    assert problems(data) == [
        "controller.ezo_period_s: must be at least 1.1 s: "
        "a read from 'ec' takes 900 ms, plus 200 ms"
    ]


def test_ezo_period_is_not_checked_without_ezo_devices():
    data = config(controller={"ezo_period_s": 0.1})
    data["device"] = data["device"][:2]
    parse_config(data)


# Shape and type checks.


def test_rejects_unknown_keys():
    data = config(extra=1)
    data["device"][0]["adress"] = 0x0A
    assert problems(data) == ["extra: unknown key", "device[0].adress: unknown key"]


def test_rejects_bool_as_integer():
    data = config()
    data["device"][2]["address"] = True
    assert problems(data) == ["device[2].address: must be an integer"]


def test_rejects_missing_required_fields():
    data = config(device=[{}])
    assert problems(data) == [
        "device[0].kind: is required",
        "device[0].name: is required",
        "device[0].bus: is required",
        "device[0].address: is required",
    ]


def test_rejects_slice_without_channels():
    data = config()
    data["device"][0]["channels"] = {}
    assert problems(data) == ["device[0].channels: must define at least one channel"]


def test_rejects_rlht_channel_without_thermocouple():
    data = config()
    data["device"][0]["channels"] = {"jacket": {"output": 2}}
    assert problems(data) == ["device[0].channels.jacket.tc: is required (1 or 2)"]


def test_rejects_output_out_of_range():
    data = config()
    data["device"][1]["channels"] = {"stirrer": {"motor": 3}}
    assert problems(data) == ["device[1].channels.stirrer.motor: must be 1 or 2"]


def test_rejects_bad_names():
    data = config()
    data["device"][2]["name"] = "Vessel Temp"
    data["device"][3]["temp_comp"] = "Vessel Temp"
    assert problems(data)[0].startswith("device[2].name: must start with a lowercase letter")


def test_rejects_non_positive_poll():
    data = config(controller={"slice_poll_s": 0})
    assert problems(data) == ["controller.slice_poll_s: must be greater than 0"]


def test_reports_every_problem_at_once():
    data = config()
    data["device"][0]["address"] = 0x80
    data["device"][3]["temp_comp"] = "missing"
    assert len(problems(data)) == 2


def test_a_device_with_a_field_error_still_gets_cross_checked():
    data = config()
    data["device"][0]["channels"] = {"ph": {"output": 1}}  # no tc, and clashes with ph
    data["device"][0]["address"] = 0x63  # clashes with ph
    data["device"][3]["temp_comp"] = "heater"
    assert problems(data) == [
        "device[0].channels.ph.tc: is required (1 or 2)",
        "device[3].name: name 'ph' is already used by channel 'ph' of 'heater'",
        "device[3].address: 0x63 on /dev/i2c-1 is already used by 'heater'",
        "device[3].temp_comp: must name an ezo-rtd device; 'heater' is rlht",
    ]


def test_a_reference_to_an_unparsed_device_is_not_reported_twice():
    data = config()
    del data["device"][2]["bus"]  # the rtd that ph compensates from
    assert problems(data) == ["device[2].bus: is required"]


def test_rejects_a_slice_named_like_another_slices_channel():
    data = config()
    data["device"][1]["name"] = "jacket"
    assert problems(data) == [
        "device[1].name: name 'jacket' is already used by channel 'jacket' of 'heater'"
    ]


def test_rejects_a_channel_named_like_its_own_slice():
    data = config()
    data["device"][0]["channels"] = {"heater": {"output": 1, "tc": 1}}
    assert problems(data) == [
        "device[0].channels.heater: name 'heater' is already used by device 'heater'"
    ]


@pytest.mark.parametrize("name", ["ph\n", "ph ", "ph\nextra"])
def test_rejects_names_with_trailing_characters(name: str):
    data = config()
    data["device"][3]["name"] = name
    assert problems(data)[0].startswith("device[3].name: must start with a lowercase letter")


def test_rejects_channel_key_with_a_newline():
    data = config()
    data["device"][0]["channels"] = {"jacket\n": {"output": 1, "tc": 1}}
    assert problems(data)[0].startswith("device[0].channels.jacket\n: must start with")


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("key", ["slice_poll_s", "ezo_period_s"])
def test_rejects_non_finite_periods(key: str, value: float):
    assert problems(config(controller={key: value})) == [
        f"controller.{key}: must be a finite number"
    ]


def test_rejects_a_poll_too_slow_for_any_watchdog():
    assert problems(config(controller={"slice_poll_s": 30.0})) == [
        "controller.slice_poll_s: must be at most 21.845 s, so that 3 polls fit in "
        "the longest watchdog timeout, 65535 ms"
    ]


def test_rejects_a_huge_poll_without_crashing():
    assert problems(config(controller={"slice_poll_s": 1e308}))[0].startswith(
        "controller.slice_poll_s: must be at most"
    )


def test_an_invalid_poll_does_not_also_fail_the_watchdog_rule():
    data = config(controller={"slice_poll_s": "x", "watchdog_timeout_ms": 300})
    assert problems(data) == ["controller.slice_poll_s: must be a number"]


def test_negative_address_message():
    data = config()
    data["device"][2]["address"] = -1
    assert problems(data) == ["device[2].address: -1 is outside the device range 0x08 to 0x77"]


def test_allow_unprotected_on_an_ezo_device_is_one_problem():
    data = config()
    data["device"][2]["allow_unprotected"] = 1
    assert problems(data) == ["device[2].allow_unprotected: unknown key"]


def test_rejects_invalid_utf8(tmp_path: Path):
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b'[server]\nhost = "\xff"\n')
    with pytest.raises(ConfigError) as e:
        load_config(bad)
    assert "not valid UTF-8" in str(e.value)


def test_rejects_invalid_toml(tmp_path: Path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[server\n")
    with pytest.raises(ConfigError) as e:
        load_config(bad)
    assert "not valid TOML" in str(e.value)


@pytest.mark.parametrize("bus", ["/dev/i2c", "i2c-1", "/dev/i2c-1x", "/dev/spidev0.0"])
def test_rejects_a_bus_that_is_not_linux_i2c(bus: str):
    data = config()
    data["device"][2]["bus"] = bus
    assert problems(data) == [f"device[2].bus: {bus!r} is not a Linux I2C bus such as /dev/i2c-1"]
