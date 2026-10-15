"""The decision engine: evaluate a ruleset against one event inside a latency budget.

Severity decides the outcome (DECLINE > REVIEW > APPROVE). If evidence could not be gathered
in time - the budget ran out, or the features query was cancelled - the outcome is raised to
the ruleset's ``fallback_outcome``. The engine can make a decision more cautious when it is
degraded, never less: a DECLINE that already fired stays a DECLINE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sentinel.clock import Budget
from sentinel.domain import Outcome, TransactionEvent, most_severe
from sentinel.rules.features import Features
from sentinel.rules.rules import RuleResult, RuleStatus
from sentinel.rules.ruleset import Ruleset

BUDGET_EXHAUSTED: Final = "budget_exhausted"
FEATURES_UNAVAILABLE: Final = "features_unavailable"


@dataclass(frozen=True, slots=True)
class EngineResult:
    outcome: Outcome
    reasons: tuple[str, ...]
    results: tuple[RuleResult, ...]
    rules_evaluated: int
    budget_exhausted: bool

    @property
    def degraded(self) -> bool:
        return self.budget_exhausted or FEATURES_UNAVAILABLE in self.reasons


def evaluate(
    event: TransactionEvent, features: Features, ruleset: Ruleset, budget: Budget
) -> EngineResult:
    results: list[RuleResult] = []
    exhausted_at: int | None = None
    starved = False

    for index, rule in enumerate(ruleset.rules):
        if exhausted_at is None and budget.expired():
            exhausted_at = index
        if exhausted_at is not None:
            results.append(
                RuleResult(rule.id, RuleStatus.SKIPPED, None, "decision budget exhausted")
            )
        elif not features.available and not rule.requires.empty:
            starved = True
            results.append(
                RuleResult(
                    rule.id, RuleStatus.SKIPPED, None, f"features unavailable: {features.error}"
                )
            )
        else:
            results.append(rule.evaluate(event, features))

    triggered = [result for result in results if result.status is RuleStatus.TRIGGERED]
    outcome = most_severe(result.outcome for result in triggered if result.outcome)
    reasons = [result.rule_id for result in triggered]
    if starved:
        reasons.append(FEATURES_UNAVAILABLE)
    if exhausted_at is not None:
        reasons.append(BUDGET_EXHAUSTED)
    if (
        starved or exhausted_at is not None
    ) and ruleset.fallback_outcome.severity > outcome.severity:
        outcome = ruleset.fallback_outcome

    return EngineResult(
        outcome=outcome,
        reasons=tuple(reasons),
        results=tuple(results),
        rules_evaluated=len(ruleset.rules) if exhausted_at is None else exhausted_at,
        budget_exhausted=exhausted_at is not None,
    )
