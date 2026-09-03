"""Clocks used by observability.

Two clocks, deliberately: a monotonic one for measuring how long something
took, and a wall clock for saying when it happened. Subtracting wall-clock
timestamps to get a duration is wrong -- the wall clock can jump -- so
durations are always measured with the monotonic clock.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Self

MonotonicNs = Callable[[], int]


def default_monotonic_ns() -> int:
    return time.perf_counter_ns()


def now_iso() -> str:
    """A UTC timestamp for display and ordering, never for measuring duration."""
    return datetime.now(UTC).isoformat()


class Stopwatch:
    """Measures elapsed milliseconds across a block.

    The monotonic source is injectable so tests can advance time without
    sleeping.
    """

    def __init__(self, monotonic_ns: MonotonicNs | None = None) -> None:
        self._monotonic_ns = monotonic_ns or default_monotonic_ns
        self._started_ns: int | None = None
        self._stopped_ns: int | None = None

        self.started_at: str | None = None
        self.completed_at: str | None = None

    def __enter__(self) -> Self:
        self.started_at = now_iso()
        self._started_ns = self._monotonic_ns()

        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stopped_ns = self._monotonic_ns()
        self.completed_at = now_iso()

    @property
    def duration_ms(self) -> int | None:
        if self._started_ns is None or self._stopped_ns is None:
            return None

        return max(0, round((self._stopped_ns - self._started_ns) / 1_000_000))
