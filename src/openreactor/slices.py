"""Open the bus and start every RLHT and DCMT slice in the config."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

from bread_crumbs_contracts.dcmt_ops import DCMT_TYPE_ID
from crumbs_i2c import Bus, CrumbsError, LinuxBus

from openreactor.config import DeviceConfig
from openreactor.controller import Event
from openreactor.dcmt import DcmtSlice
from openreactor.ezo import Outcome, Result
from openreactor.rlht import CrumbsPort, RlhtSlice, SliceError, _describe

log = logging.getLogger(__name__)

Slice = RlhtSlice | DcmtSlice


def _open_linux_bus(path: str) -> Bus:
    return LinuxBus(path)


def release(slices: Sequence[Slice]) -> None:
    """Release each started DCMT at a clean shutdown: safe state, then its
    watchdog disarmed, so a latching slice is not left to trip.

    Call it only once the controller has stopped and stop-all has been
    sent: a controller still running could drive a motor after its
    watchdog was disarmed. A slice that fails keeps its watchdog armed,
    which trips and brakes it.
    """
    for s in slices:
        if isinstance(s, DcmtSlice) and s.started:
            try:
                s.release()
            except (CrumbsError, OSError) as e:
                log.warning("%s: not released at shutdown: %s", s.name, _describe(e))


@contextmanager
def opened_slices(
    devices: Sequence[DeviceConfig],
    *,
    watchdog_timeout_ms: int,
    poll_s: float,
    open_bus: Callable[[str], Bus] = _open_linux_bus,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    wall: Callable[[], float] = time.time,
) -> Iterator[tuple[list[Slice], list[Result | Event]]]:
    """Open each slice's bus and start each slice.

    Slices on the same bus path share one bus. Yield every slice that
    answered as its kind, so stop-all covers it whether or not its start-up
    finished (``started`` says which to poll), with the start-up events and
    problems. The buses close on exit: send stop-all before that, then
    ``release`` the slices once nothing else drives them.
    """
    buses: dict[str, Bus] = {}
    slices: list[Slice] = []
    reports: list[Result | Event] = []
    try:
        for d in devices:
            if d.kind not in ("rlht", "dcmt"):
                continue
            try:
                if d.bus not in buses:
                    buses[d.bus] = open_bus(d.bus)
                s: Slice
                if d.kind == "rlht":
                    s = RlhtSlice(
                        d,
                        CrumbsPort(buses[d.bus], d.address, sleep=sleep),
                        watchdog_timeout_ms=watchdog_timeout_ms,
                        poll_s=poll_s,
                        clock=clock,
                        sleep=sleep,
                        wall=wall,
                    )
                else:
                    s = DcmtSlice(
                        d,
                        CrumbsPort(buses[d.bus], d.address, type_id=DCMT_TYPE_ID, sleep=sleep),
                        watchdog_timeout_ms=watchdog_timeout_ms,
                        poll_s=poll_s,
                        clock=clock,
                        wall=wall,
                    )
                try:
                    reports += s.start()
                finally:
                    if s.identified:
                        slices.append(s)
            except (SliceError, CrumbsError, OSError, ValueError) as e:
                reports.append(Result(d.name, Outcome.ERROR, detail=_describe(e)))
        yield slices, reports
    finally:
        for bus in buses.values():
            close = getattr(bus, "close", None)
            if close is not None:
                close()
