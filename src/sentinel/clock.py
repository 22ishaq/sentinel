"""Time sources and latency budgets.

Decisions are made against a :class:`Deadline` derived from the monotonic clock. The same
deadline caps how long PostgreSQL may spend on the decision's lock and feature queries,
and the rule engine checks it between rules. :class:`StepBudget` replays a past decision
whose budget ran out after a known number of rules, so the replay skips the same rules.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


class Budget(Protocol):
    def expired(self) -> bool: ...


class Deadline:
    """A point on the monotonic clock after which the decision must stop gathering evidence."""

    def __init__(self, clock: Clock, expires_at_ns: int) -> None:
        self._clock = clock
        self.expires_at_ns = expires_at_ns

    @classmethod
    def after(cls, clock: Clock, budget_ns: int, *, start_ns: int | None = None) -> Deadline:
        start = clock.monotonic_ns() if start_ns is None else start_ns
        return cls(clock, start + budget_ns)

    def remaining_ns(self) -> int:
        return max(0, self.expires_at_ns - self._clock.monotonic_ns())

    def remaining_ms(self) -> int:
        """Remaining time rounded up to whole milliseconds, never below 1 (for SQL timeouts)."""
        return max(1, -(-self.remaining_ns() // 1_000_000))

    def expired(self) -> bool:
        return self.remaining_ns() == 0


class StepBudget:
    """Expires after ``steps`` checks; ``None`` never expires. Used to replay decisions."""

    def __init__(self, steps: int | None) -> None:
        self._left = steps

    def expired(self) -> bool:
        if self._left is None:
            return False
        if self._left <= 0:
            return True
        self._left -= 1
        return False
