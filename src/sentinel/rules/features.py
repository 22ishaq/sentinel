"""Behavioural features: what the rules need to know about an account's recent history.

Rules declare a :class:`FeatureRequest`; the store answers it in a single round trip under
the decision deadline. If the deadline wins, :meth:`Features.unavailable` records why, and
rules that needed the features are skipped rather than evaluated on guesses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sentinel.errors import JsonValue


@dataclass(frozen=True, slots=True)
class FeatureRequest:
    attempt_windows: frozenset[int] = frozenset()
    spend_windows: frozenset[int] = frozenset()
    merchant_history: bool = False

    def __or__(self, other: FeatureRequest) -> FeatureRequest:
        return FeatureRequest(
            self.attempt_windows | other.attempt_windows,
            self.spend_windows | other.spend_windows,
            self.merchant_history or other.merchant_history,
        )

    @property
    def empty(self) -> bool:
        return not (self.attempt_windows or self.spend_windows or self.merchant_history)


@dataclass(frozen=True, slots=True)
class Features:
    """Feature values for one event, keyed by window length in seconds.

    ``attempts``: prior events from the account received within the window, any outcome.
    ``approved_spend``: prior *approved* minor units in the event's currency within the window.
    ``merchant_seen``: whether the account has transacted with this merchant before.
    """

    attempts: Mapping[int, int] = field(default_factory=dict)
    approved_spend: Mapping[int, int] = field(default_factory=dict)
    merchant_seen: bool | None = None
    available: bool = True
    error: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> Features:
        return cls(available=False, error=reason)

    def to_json(self) -> dict[str, JsonValue]:
        # Spend is written as a string: minor-unit sums can exceed 2**53, which JavaScript
        # consumers of the audit API would otherwise silently round.
        return {
            "available": self.available,
            "error": self.error,
            "attempts": {str(window): count for window, count in sorted(self.attempts.items())},
            "approved_spend": {
                str(window): str(total) for window, total in sorted(self.approved_spend.items())
            },
            "merchant_seen": self.merchant_seen,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> Features:
        attempts = data.get("attempts", {})
        spend = data.get("approved_spend", {})
        seen = data.get("merchant_seen")
        error = data.get("error")
        if not (isinstance(attempts, dict) and isinstance(spend, dict)):
            raise TypeError("malformed features record")
        return cls(
            attempts={int(window): int(count) for window, count in attempts.items()},
            approved_spend={int(window): int(total) for window, total in spend.items()},
            merchant_seen=seen if isinstance(seen, bool) else None,
            available=data.get("available") is True,
            error=error if isinstance(error, str) else None,
        )
