import pytest
from fakes import FakeClock, FakeDcmt, FakeI2cBus, FakeRlht

from openreactor.config import ChannelConfig, DeviceConfig
from openreactor.controller import Controller, Event
from openreactor.dcmt import DcmtSlice
from openreactor.ezo import EzoReader
from openreactor.profile import parse_profile
from openreactor.rlht import CrumbsPort, RlhtSlice
from openreactor.runner import ProfileRunner

JACKET = ChannelConfig(name="jacket", label="Jacket", output=1, tc=1)
LID = ChannelConfig(name="lid", label="Lid", output=2, tc=2)
STIRRER = ChannelConfig(name="stirrer", label="Stirrer", output=1)
HEATER = DeviceConfig("heater", "rlht", "/dev/i2c-1", 0x0A, channels=(JACKET, LID))
MOTORS = DeviceConfig("motors", "dcmt", "/dev/i2c-1", 0x14, channels=(STIRRER,))


class Rig:
    """A heater and a motor slice on a fake bus, a controller, and a runner."""

    def __init__(self, steps: dict[str, list[dict]]):
        self.clock = FakeClock()
        self.rlht = FakeRlht()
        self.dcmt = FakeDcmt()
        bus = FakeI2cBus({0x0A: self.rlht, 0x14: self.dcmt})
        self.heater = RlhtSlice(
            HEATER,
            CrumbsPort(bus, 0x0A, sleep=self.clock.sleep),
            watchdog_timeout_ms=5000,
            poll_s=1.0,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.motors = DcmtSlice(
            MOTORS,
            CrumbsPort(bus, 0x14, type_id=0x02, sleep=self.clock.sleep),
            watchdog_timeout_ms=5000,
            poll_s=1.0,
            clock=self.clock,
        )
        self.heater.start()
        self.motors.start()
        profile = parse_profile({"version": 1, "name": "test", "channels": steps})
        self.ended: list[tuple[str, str]] = []
        channels = {
            "jacket": (HEATER, JACKET, self.heater),
            "lid": (HEATER, LID, self.heater),
            "stirrer": (MOTORS, STIRRER, self.motors),
        }
        self.runner = ProfileRunner(
            profile,
            channels,
            step_s=1.0,
            finished=lambda status, why: self.ended.append((status, why)),
        )
        self.seen: list = []
        self.controller = Controller(
            EzoReader([]),
            [self.heater, self.motors],
            polled=[self.heater, self.motors, self.runner],
            ezo_period_s=2.0,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        self.controller.subscribe(self.seen.append)
        self.controller.subscribe(self.runner.notice)  # as the service does

    def tick(self, seconds: float) -> None:
        for _ in range(round(seconds / 0.1)):
            self.controller.step()
            self.clock.now = round(self.clock.now + 0.1, 9)

    def events(self, kind: str) -> list:
        return [e for e in self.seen if getattr(e, "kind", None) == kind]


def test_a_ramp_is_stepped_at_each_poll_period_and_ends_safe():
    rig = Rig({"jacket": [{"set": 30.0}, {"ramp": 40.0, "over": "10s"}, {"hold": "5s"}]})
    rig.tick(0.1)
    assert rig.rlht.setpoints == [300, 0]
    rig.tick(5.0)
    assert rig.rlht.setpoints == [350, 0]  # halfway up the ramp
    rig.tick(5.0)
    assert rig.rlht.setpoints == [400, 0]
    assert not rig.ended
    rig.tick(6.0)
    # At the end, the channel it drove goes to its safe state.
    assert rig.rlht.setpoints == [0, 0]
    assert rig.ended == [("completed", "the profile finished")]
    assert rig.events("profile-end")
    sent = [e.details for e in rig.events("setpoint")]
    assert sent[0] == "30 °C" and sent[-1] == "0 °C"
    assert all(e.source == "profile" for e in rig.events("setpoint"))


def test_a_motor_channel_runs_a_duty():
    rig = Rig({"stirrer": [{"set": 50.0}, {"hold": "3s"}]})
    rig.tick(0.1)
    assert rig.dcmt.pwm == [128, 0]
    assert rig.events("setpoint")[0].details == "50.2 %"
    rig.tick(4.0)
    assert (rig.dcmt.pwm, rig.dcmt.brakes) == ([0, 0], [1, 1])
    assert rig.ended[0][0] == "completed"


def test_a_value_that_does_not_change_is_not_sent_again():
    rig = Rig({"jacket": [{"set": 30.0}, {"hold": "5s"}]})
    rig.tick(4.0)
    assert len(rig.events("setpoint")) == 1


def test_nothing_is_sent_before_a_channels_first_value():
    rig = Rig({"jacket": [{"hold": "3s"}, {"set": 30.0}, {"hold": "2s"}]})
    rig.tick(2.0)
    assert rig.rlht.setpoints == [0, 0] and not rig.events("setpoint")
    rig.tick(2.0)
    assert rig.rlht.setpoints == [300, 0]


@pytest.mark.parametrize(
    "outside",
    [
        "stop-all",
        "e-stop",
        "reboot",
        "operator",
    ],
)
def test_a_channel_changed_outside_the_profile_aborts_it_and_nothing_resumes(outside):
    rig = Rig(
        {
            "jacket": [{"set": 30.0}, {"hold": "60s"}],
            "stirrer": [{"set": 40.0}, {"hold": "60s"}],
        }
    )
    rig.tick(2.0)
    assert rig.rlht.setpoints == [300, 0] and rig.dcmt.pwm[0] != 0
    if outside == "stop-all":
        rig.controller.stop_all("user")
    elif outside == "e-stop":
        rig.rlht.flags = 0x01
    elif outside == "reboot":
        rig.rlht.reboot()  # a power-cut e-stop, say
    else:
        rig.heater.set_setpoint(1, 450)
    rig.tick(8.0)
    [abort] = rig.events("profile-abort")
    reasons = {
        "stop-all": "stop-all was sent",
        "e-stop": "the e-stop on heater was pressed",
        "reboot": "heater: slice-reboot",
        "operator": "jacket no longer runs what the profile set",
    }
    assert abort.details.startswith(reasons[outside])
    assert rig.ended and rig.ended[0][0] == "stopped"
    rig.rlht.flags = 0
    rig.tick(10.0)
    # The motor it also drove went safe, and nothing was sent back to the heater.
    assert (rig.dcmt.pwm, rig.dcmt.brakes) == ([0, 0], [1, 1])
    if outside != "operator":
        assert rig.rlht.setpoints == [0, 0]
    assert len(rig.events("profile-abort")) == 1


def test_an_unreachable_slice_aborts_the_profile():
    rig = Rig({"jacket": [{"set": 30.0}, {"ramp": 40.0, "over": "60s"}]})
    rig.tick(2.0)
    rig.rlht.faults = [OSError(121, "Remote I/O error")] * 1000
    rig.tick(6.0)
    [abort] = rig.events("profile-abort")
    assert "unreachable" in abort.details
    assert rig.ended[0][0] == "stopped"


def test_an_abort_while_a_check_is_pending_does_not_leave_the_heater_on():
    """The 0 is refused while the slice is checked: it is wanted anyway, so
    the re-assert after the trip sends 0, not the profile's value."""
    rig = Rig({"jacket": [{"set": 30.0}, {"hold": "60s"}]})
    rig.tick(2.0)
    rig.rlht.trip()
    for _ in range(50):  # until the poll finds the drift and owes a check
        rig.tick(0.1)
        if rig.heater._drift_against is not None:
            break
    assert rig.heater._drift_against is not None
    out = rig.runner.abort("aborted by the operator")
    [safe] = [e for e in out if isinstance(e, Event) and e.kind == "setpoint"]
    assert "set to 0 as wanted" in safe.result
    rig.tick(10.0)
    assert rig.heater.setpoints_deci == [0, 0] and rig.rlht.setpoints == [0, 0]


def test_a_bus_outage_aborts_the_profile_and_the_heater_does_not_resume():
    rig = Rig({"jacket": [{"set": 30.0}, {"ramp": 40.0, "over": "60s"}]})
    rig.tick(2.0)
    rig.rlht.faults = [OSError(121, "Remote I/O error")] * 1000
    rig.tick(6.0)
    assert rig.events("profile-abort")
    rig.rlht.faults = []
    rig.tick(10.0)
    assert rig.heater.setpoints_deci == [0, 0] and rig.rlht.setpoints == [0, 0]


@pytest.mark.parametrize("how", ["e-stop", "stop-all"])
def test_a_stop_before_a_channel_is_first_set_aborts_the_profile(how):
    rig = Rig({"jacket": [{"hold": "5s"}, {"set": 30.0}, {"hold": "60s"}]})
    rig.tick(1.0)
    if how == "e-stop":
        rig.rlht.flags = 0x01
        rig.tick(3.0)
        rig.rlht.flags = 0
    else:
        rig.controller.stop_all("slice")
    rig.tick(10.0)
    assert rig.events("profile-abort") and rig.rlht.setpoints == [0, 0]


def test_a_stop_while_a_channel_is_at_0_aborts_the_profile():
    rig = Rig({"jacket": [{"set": 0.0}, {"hold": "5s"}, {"set": 30.0}, {"hold": "60s"}]})
    rig.tick(1.0)
    rig.controller.stop_all("slice")
    rig.tick(10.0)
    [abort] = rig.events("profile-abort")
    assert abort.details == "stop-all was sent" and rig.rlht.setpoints == [0, 0]


def test_a_reboot_during_a_hold_aborts_the_profile():
    rig = Rig({"jacket": [{"set": 30.0}, {"hold": "60s"}]})
    rig.tick(2.0)
    rig.rlht.reboot()
    rig.tick(6.0)
    [abort] = rig.events("profile-abort")
    assert "slice-reboot" in abort.details


def test_a_plain_trip_does_not_abort_the_profile():
    rig = Rig({"jacket": [{"set": 30.0}, {"hold": "60s"}]})
    rig.tick(2.0)
    rig.rlht.trip()
    rig.tick(8.0)
    assert not rig.events("profile-abort") and rig.rlht.setpoints == [300, 0]


def test_one_failed_read_before_a_send_is_tried_again():
    rig = Rig({"jacket": [{"set": 30.0}, {"ramp": 40.0, "over": "20s"}]})
    rig.tick(2.05)
    rig.rlht.corrupt_replies = {0x7D}  # GET_WATCHDOG, for one step
    rig.tick(1.0)
    rig.rlht.corrupt_replies = set()
    rig.tick(3.0)
    assert not rig.events("profile-abort")
    assert rig.rlht.setpoints[0] > 320


def test_a_slice_being_checked_is_tried_again_at_the_next_step():
    rig = Rig({"jacket": [{"set": 30.0}, {"ramp": 40.0, "over": "20s"}]})
    rig.tick(2.0)
    rig.rlht.trip_count += 1  # a trip no poll has seen yet: the next send is refused
    rig.tick(4.0)
    assert not rig.events("profile-abort")
    assert rig.rlht.setpoints[0] > 300  # the ramp went on once the check re-asserted


def test_abort_sends_each_touched_channel_safe_once_and_is_idempotent():
    rig = Rig(
        {
            "jacket": [{"set": 30.0}, {"hold": "60s"}],
            "lid": [{"hold": "30s"}, {"set": 20.0}, {"hold": "30s"}],
        }
    )
    rig.tick(2.0)
    out = rig.runner.abort("aborted by the operator")
    assert rig.rlht.setpoints == [0, 0]
    sent = [e for e in out if isinstance(e, Event) and e.kind == "setpoint"]
    assert [(e.channel, e.details) for e in sent] == [("jacket", "0 °C")]  # lid never set
    assert rig.runner.abort("again") == []
    assert rig.ended == [("stopped", "aborted by the operator")]
