"""Injectable clock module for deterministic time handling.

Nothing in the codebase outside of this module may access the system wall clock directly.
All timestamps (received_at, job leases, retry backoff, freshness evaluation,
and as_of defaults) MUST be obtained through this interface.
"""

import os
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    """Abstract clock interface providing UTC datetime."""

    @abstractmethod
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        raise NotImplementedError


class SystemClock(Clock):
    """Standard production clock returning real system UTC time (or anchored time if CLOCK_ANCHOR is set)."""

    def now(self) -> datetime:
        anchor = os.environ.get("CLOCK_ANCHOR")
        if anchor:
            dt = datetime.fromisoformat(anchor.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        return datetime.now(UTC)


class FrozenClock(Clock):
    """Deterministic, settable clock for unit and integration testing."""

    def __init__(self, current_time: datetime | str) -> None:
        if isinstance(current_time, str):
            dt = datetime.fromisoformat(current_time.replace("Z", "+00:00"))
        else:
            dt = current_time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        self._current_time: datetime = dt

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime | str) -> None:
        """Set the clock to a specific time."""
        if isinstance(new_time, str):
            dt = datetime.fromisoformat(new_time.replace("Z", "+00:00"))
        else:
            dt = new_time
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        self._current_time = dt

    def advance(self, duration: timedelta | float) -> None:
        """Advance the frozen clock by a timedelta or seconds."""
        if isinstance(duration, (int, float)):
            delta = timedelta(seconds=duration)
        else:
            delta = duration
        self._current_time += delta


# Default global clock instance
_GLOBAL_CLOCK: Clock = SystemClock()


def get_clock() -> Clock:
    """Obtain the currently configured clock instance."""
    return _GLOBAL_CLOCK


def set_clock(clock: Clock) -> None:
    """Set the active clock instance (primarily for test harnesses)."""
    global _GLOBAL_CLOCK
    _GLOBAL_CLOCK = clock


def reset_clock() -> None:
    """Reset the active clock to the standard SystemClock."""
    global _GLOBAL_CLOCK
    _GLOBAL_CLOCK = SystemClock()


@contextmanager
def frozen_time(initial_time: datetime | str) -> Generator[FrozenClock, None, None]:
    """Context manager to freeze and manipulate time during test blocks."""
    clock = FrozenClock(initial_time)
    previous_clock = get_clock()
    set_clock(clock)
    try:
        yield clock
    finally:
        set_clock(previous_clock)
