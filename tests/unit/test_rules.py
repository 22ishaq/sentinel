from __future__ import annotations

from typing import Any

import pytest

from sentinel.domain import Outcome, TransactionEvent, parse_transaction
from sentinel.rules import Features, RuleResult, RuleStatus
from tests import support


def _evaluate(rule: dict[str, Any], features: Features | None = None, **event: Any) -> RuleResult:
    parsed: TransactionEvent = parse_transaction(support.event(**event))
    (only,) = support.ruleset(rule).rules
    return only.evaluate(parsed, features or Features())


@pytest.mark.parametrize(
    ("amount", "currency", "status"),
    [
        ("500.00", "GBP", RuleStatus.PASSED),
        ("500.01", "GBP", RuleStatus.TRIGGERED),
        ("500", "JPY", RuleStatus.NOT_APPLICABLE),
    ],
)
def test_amount_above(amount: str, currency: str, status: RuleStatus) -> None:
    result = _evaluate(support.HIGH_VALUE, amount=amount, currency=currency)
    assert result.status is status
    if status is RuleStatus.TRIGGERED:
        assert result.outcome is Outcome.REVIEW
        assert result.evidence == {"amount": "500.01", "limit": "500.00", "currency": "GBP"}


def test_merchant_category_and_country_block_lists() -> None:
    gambling = support.merchant(category="7995")
    assert _evaluate(support.BLOCKED_CATEGORY, merchant=gambling).status is RuleStatus.TRIGGERED
    assert _evaluate(support.BLOCKED_CATEGORY).status is RuleStatus.PASSED
    blocked = support.merchant(country="KP")
    assert _evaluate(support.BLOCKED_COUNTRY, merchant=blocked).status is RuleStatus.TRIGGERED
    assert _evaluate(support.BLOCKED_COUNTRY).status is RuleStatus.PASSED


@pytest.mark.parametrize(("prior", "status"), [(2, RuleStatus.PASSED), (3, RuleStatus.TRIGGERED)])
def test_attempt_velocity(prior: int, status: RuleStatus) -> None:
    result = _evaluate(support.RAPID_ATTEMPTS, Features(attempts={300: prior}))
    assert result.status is status
    assert result.evidence["prior_attempts"] == prior


@pytest.mark.parametrize(
    ("prior_minor", "amount", "status"),
    [(90_000, "100.00", RuleStatus.PASSED), (90_000, "100.01", RuleStatus.TRIGGERED)],
)
def test_spend_velocity(prior_minor: int, amount: str, status: RuleStatus) -> None:
    features = Features(approved_spend={86400: prior_minor})
    result = _evaluate(support.DAILY_SPEND, features, amount=amount)
    assert result.status is status
    assert result.evidence["prior_approved_spend"] == "900.00"


def test_spend_velocity_without_a_limit_for_the_currency() -> None:
    features = Features(approved_spend={86400: 0})
    result = _evaluate(support.DAILY_SPEND, features, amount="1", currency="JPY")
    assert result.status is RuleStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("seen", "amount", "status"),
    [
        (False, "50.01", RuleStatus.TRIGGERED),
        (False, "50.00", RuleStatus.PASSED),
        (True, "5000.00", RuleStatus.PASSED),
    ],
)
def test_new_merchant_amount(seen: bool, amount: str, status: RuleStatus) -> None:
    result = _evaluate(support.NEW_MERCHANT, Features(merchant_seen=seen), amount=amount)
    assert result.status is status


def test_new_merchant_amount_without_a_limit_for_the_currency() -> None:
    result = _evaluate(
        support.NEW_MERCHANT, Features(merchant_seen=False), currency="JPY", amount="9"
    )
    assert result.status is RuleStatus.NOT_APPLICABLE


@pytest.mark.parametrize(
    ("channel", "country", "status"),
    [
        ("ecommerce", "FR", RuleStatus.TRIGGERED),
        ("ecommerce", "GB", RuleStatus.PASSED),
        ("card_present", "FR", RuleStatus.NOT_APPLICABLE),
    ],
)
def test_cross_border(channel: str, country: str, status: RuleStatus) -> None:
    result = _evaluate(
        support.CROSS_BORDER, channel=channel, merchant=support.merchant(country=country)
    )
    assert result.status is status


def test_results_serialise_for_the_audit_record() -> None:
    result = _evaluate(support.HIGH_VALUE, amount="900.00")
    assert result.to_json() == {
        "rule_id": "high_value",
        "status": "triggered",
        "outcome": "REVIEW",
        "detail": "amount 900.00 GBP exceeds 500.00 GBP",
        "evidence": {"amount": "900.00", "limit": "500.00", "currency": "GBP"},
    }
