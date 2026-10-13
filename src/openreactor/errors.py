"""Map the errors an action can raise to the HTTP status that reports them.

The API answers them as JSON and the UI as a notice, with the same status
and the same text.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import TimeoutError as FutureTimeout

from openreactor.controller import ControllerClosed
from openreactor.ezo import EzoDeviceError, EzoStatusError
from openreactor.service import (
    Conflict,
    DeviceFailed,
    Invalid,
    NotFound,
    Unavailable,
    Unreachable,
)
from openreactor.storage import StorageError

ERRORS: tuple[tuple[type[Exception], int], ...] = (
    (NotFound, 404),
    (Conflict, 409),
    (Unavailable, 409),
    (Invalid, 422),
    (Unreachable, 503),
    # The circuit refused the command or could not be reached.
    (EzoStatusError, 502),
    (EzoDeviceError, 502),
    (DeviceFailed, 502),
    (StorageError, 503),
    (sqlite3.Error, 503),
    (ControllerClosed, 503),
    (FutureTimeout, 504),
)

# Every error in ERRORS, for an ``except`` clause.
HANDLED: tuple[type[Exception], ...] = tuple(e for e, _ in ERRORS)


def error_detail(exc: Exception) -> str:
    if isinstance(exc, EzoStatusError):
        return f"the circuit answered {exc.outcome.value}"
    return str(exc) or "the controller did not finish in time"


def error_status(exc: Exception) -> int:
    """Return the status of the first entry in ERRORS that ``exc`` matches."""
    return next(c for e, c in ERRORS if isinstance(exc, e))
