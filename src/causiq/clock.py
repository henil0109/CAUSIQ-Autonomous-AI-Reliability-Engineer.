"""The Clock port and its two implementations (ADR-0004).

Maturity: hardened.

Why a clock is a port: every evidence record and every audit entry carries a
timestamp, and invariant I6 requires two offline runs to produce byte-identical
audit journals. That is impossible if the code reads the wall clock directly.
Injecting time is what makes the audit trail assertable rather than merely
plausible.

Wall-clock time (`now`) and elapsed time (`monotonic`) are separate methods on
purpose. Timestamps must be UTC-aware and human-meaningful; budget deadlines must
be immune to system clock adjustments. Using `now()` for a deadline is a real bug
in long-running processes.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Port: the only sanctioned source of time in Causiq."""

    def now(self) -> datetime:
        """Current wall-clock time. Always timezone-aware, always UTC."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds, for measuring elapsed time and deadlines."""
        ...


class SystemClock:
    """Production implementation."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FrozenClock:
    """Deterministic implementation for tests.

    Time only moves when the test says so - either by an explicit `advance()`, or
    by the fixed `step` applied after each `now()` read when a test needs
    ordered-but-predictable timestamps.
    """

    def __init__(
        self,
        start: datetime | None = None,
        *,
        step: timedelta = timedelta(0),
    ) -> None:
        moment = start if start is not None else datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)
        if moment.tzinfo is None:
            msg = "FrozenClock requires a timezone-aware start time"
            raise ValueError(msg)
        self._now = moment.astimezone(UTC)
        self._step = step
        self._monotonic = 0.0

    def now(self) -> datetime:
        current = self._now
        if self._step:
            self.advance(self._step)
        return current

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, delta: timedelta) -> None:
        """Move both wall-clock and monotonic time forward by `delta`."""
        self._now += delta
        self._monotonic += delta.total_seconds()
