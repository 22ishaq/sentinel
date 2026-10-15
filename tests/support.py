"""Shared test helpers: payload builders, test rulesets and a controllable clock."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sentinel.canonical import format_timestamp
from sentinel.rules import Ruleset, load_ruleset
from sentinel.store.db import Pool


def event(**overrides: Any) -> dict[str, Any]:
    """A valid event payload; keyword arguments replace top-level fields."""
    payload: dict[str, Any] = {
        "event_id": f"evt_{uuid4().hex}",
        "occurred_at": format_timestamp(datetime.now(UTC) - timedelta(seconds=5)),
        "account": {"id": "acc_alice", "country": "GB"},
        "amount": "25.00",
        "currency": "GBP",
        "channel": "card_present",
        "merchant": {"id": "m_grocer", "category_code": "5411", "country": "GB"},
    }
    payload.update(copy.deepcopy(overrides))
    return payload


def merchant(
    merchant_id: str = "m_grocer", category: str = "5411", country: str = "GB"
) -> dict[str, str]:
    return {"id": merchant_id, "category_code": category, "country": country}


def account(account_id: str = "acc_alice", country: str = "GB") -> dict[str, str]:
    return {"id": account_id, "country": country}


def ruleset(*rules: dict[str, Any], fallback: str = "REVIEW", name: str = "test") -> Ruleset:
    return load_ruleset({"name": name, "fallback_outcome": fallback, "rules": list(rules)})


BLOCKED_COUNTRY = {
    "id": "blocked_country",
    "type": "merchant_country",
    "outcome": "DECLINE",
    "countries": ["KP"],
}
BLOCKED_CATEGORY = {
    "id": "blocked_category",
    "type": "merchant_category",
    "outcome": "DECLINE",
    "categories": ["7995"],
}
CEILING = {
    "id": "ceiling",
    "type": "amount_above",
    "outcome": "DECLINE",
    "limits": {"GBP": "10000.00", "JPY": "1000000"},
}
HIGH_VALUE = {
    "id": "high_value",
    "type": "amount_above",
    "outcome": "REVIEW",
    "limits": {"GBP": "500.00"},
}
DAILY_SPEND = {
    "id": "daily_spend",
    "type": "spend_velocity",
    "outcome": "DECLINE",
    "window_seconds": 86400,
    "limits": {"GBP": "1000.00"},
}
RAPID_ATTEMPTS = {
    "id": "rapid_attempts",
    "type": "attempt_velocity",
    "outcome": "REVIEW",
    "window_seconds": 300,
    "max_prior_attempts": 3,
}
NEW_MERCHANT = {
    "id": "new_merchant",
    "type": "new_merchant_amount",
    "outcome": "REVIEW",
    "limits": {"GBP": "50.00"},
}
CROSS_BORDER = {
    "id": "cross_border",
    "type": "cross_border",
    "outcome": "REVIEW",
    "channels": ["ecommerce"],
}
ALL_RULES = (
    BLOCKED_COUNTRY,
    BLOCKED_CATEGORY,
    CEILING,
    DAILY_SPEND,
    RAPID_ATTEMPTS,
    HIGH_VALUE,
    NEW_MERCHANT,
    CROSS_BORDER,
)


class FakeClock:
    """Wall clock anchored at real time; monotonic clock fully under test control.

    ``step_ns`` is added after every monotonic read, simulating work that takes time.
    """

    def __init__(self, step_ns: int = 0) -> None:
        self._start = datetime.now(UTC)
        self._mono = 0
        self.step_ns = step_ns

    def now(self) -> datetime:
        return self._start + timedelta(microseconds=self._mono // 1_000)

    def monotonic_ns(self) -> int:
        value = self._mono
        self._mono += self.step_ns
        return value

    def advance(self, ns: int) -> None:
        self._mono += ns


def count(pool: Pool, table: str, where: str = "true") -> int:
    with pool.connection() as conn:
        row = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()
    assert row is not None
    return int(row[0])


def scalar(pool: Pool, query: str, *params: object) -> Any:
    with pool.connection() as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def execute(pool: Pool, query: str, *params: object) -> None:
    with pool.connection() as conn:
        conn.execute(query, params)


def json_text(value: object) -> str:
    return json.dumps(value)
