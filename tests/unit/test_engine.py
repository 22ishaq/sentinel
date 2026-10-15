from __future__ import annotations

from typing import Any

import pytest

from sentinel.clock import StepBudget
from sentinel.domain import Outcome, TransactionEvent, parse_transaction
from sentinel.rules import (
    BUDGET_EXHAUSTED,
    FEATURES_UNAVAILABLE,
    FeatureRequest,
    Features,
    RuleStatus,
    evaluate,
)
from tests import support

HISTORY = Features(attempts={300: 0}, approved_spend={86400: 0}, merchant_seen=True)


def _event(**overrides: Any) -> TransactionEvent:
    return parse_transaction(support.event(**overrides))


def _statuses(result: Any) -> dict[str, RuleStatus]:
    return {r.rule_id: r.status for r in result.results}


def test_nothing_triggered_means_approve() -> None:
    result = evaluate(_event(), HISTORY, support.ruleset(*support.ALL_RULES), StepBudget(None))
    assert result.outcome is Outcome.APPROVE
    assert result.reasons == ()
    assert result.rules_evaluated == len(support.ALL_RULES)
    assert not result.degraded


def test_decline_outranks_review() -> None:
    ruleset = support.ruleset(support.HIGH_VALUE, support.BLOCKED_COUNTRY)
    event = _event(amount="600.00", merchant=support.merchant(country="KP"))
    result = evaluate(event, HISTORY, ruleset, StepBudget(None))
    assert result.outcome is Outcome.DECLINE
    assert result.reasons == ("high_value", "blocked_country")


def test_missing_features_skip_only_the_rules_that_need_them() -> None:
    ruleset = support.ruleset(support.CEILING, support.DAILY_SPEND, support.NEW_MERCHANT)
    result = evaluate(_event(), Features.unavailable("timeout"), ruleset, StepBudget(None))
    assert _statuses(result) == {
        "ceiling": RuleStatus.PASSED,
        "daily_spend": RuleStatus.SKIPPED,
        "new_merchant": RuleStatus.SKIPPED,
    }
    assert result.outcome is Outcome.REVIEW
    assert result.reasons == (FEATURES_UNAVAILABLE,)
    assert result.degraded


def test_degraded_decisions_are_never_softened() -> None:
    ruleset = support.ruleset(support.BLOCKED_CATEGORY, support.DAILY_SPEND, fallback="REVIEW")
    event = _event(merchant=support.merchant(category="7995"))
    result = evaluate(event, Features.unavailable("timeout"), ruleset, StepBudget(None))
    assert result.outcome is Outcome.DECLINE


def test_a_fail_open_fallback_still_records_why() -> None:
    ruleset = support.ruleset(support.DAILY_SPEND, fallback="APPROVE")
    result = evaluate(_event(), Features.unavailable("timeout"), ruleset, StepBudget(None))
    assert result.outcome is Outcome.APPROVE
    assert result.reasons == (FEATURES_UNAVAILABLE,)


def test_a_ruleset_without_history_rules_is_not_degraded_by_missing_features() -> None:
    ruleset = support.ruleset(support.CEILING)
    result = evaluate(_event(), Features.unavailable("timeout"), ruleset, StepBudget(None))
    assert result.outcome is Outcome.APPROVE
    assert not result.degraded


@pytest.mark.parametrize("steps", [0, 1, 2])
def test_the_budget_stops_evaluation_between_rules(steps: int) -> None:
    ruleset = support.ruleset(support.BLOCKED_COUNTRY, support.CEILING, support.HIGH_VALUE)
    result = evaluate(_event(amount="600.00"), HISTORY, ruleset, StepBudget(steps))
    assert result.budget_exhausted
    assert result.rules_evaluated == steps
    assert [r.status is RuleStatus.SKIPPED for r in result.results] == [
        index >= steps for index in range(3)
    ]
    assert result.reasons[-1] == BUDGET_EXHAUSTED
    assert result.outcome is Outcome.REVIEW


def test_feature_requests_combine_across_rules() -> None:
    ruleset = support.ruleset(*support.ALL_RULES)
    assert ruleset.requires == FeatureRequest(
        attempt_windows=frozenset({300}),
        spend_windows=frozenset({86400}),
        merchant_history=True,
    )
    assert support.ruleset(support.CEILING).requires.empty


def test_features_round_trip_through_json() -> None:
    features = Features(
        attempts={60: 2, 300: 5}, approved_spend={86400: 10**30}, merchant_seen=False
    )
    encoded = features.to_json()
    assert encoded["approved_spend"] == {"86400": str(10**30)}
    assert Features.from_json(encoded) == features
    assert Features.from_json(Features.unavailable("slow").to_json()) == Features.unavailable(
        "slow"
    )
    with pytest.raises(TypeError):
        Features.from_json({"attempts": [], "approved_spend": {}})
