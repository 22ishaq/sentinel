"""Rule types. Each rule is a pure function of (event, features) that explains itself.

A :class:`RuleResult` carries the evidence the rule looked at, so the audit trail shows not
just *that* a rule fired but the exact values it compared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from sentinel.domain import Channel, Outcome, TransactionEvent
from sentinel.errors import JsonValue
from sentinel.money import Money
from sentinel.rules.features import FeatureRequest, Features


class RuleStatus(StrEnum):
    TRIGGERED = "triggered"
    PASSED = "passed"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule_id: str
    status: RuleStatus
    outcome: Outcome | None
    detail: str
    evidence: Mapping[str, JsonValue] = field(default_factory=dict)

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "outcome": self.outcome.value if self.outcome else None,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


class Rule(ABC):
    id: str
    outcome: Outcome

    @property
    def requires(self) -> FeatureRequest:
        return FeatureRequest()

    @abstractmethod
    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult: ...

    def _triggered(self, detail: str, evidence: Mapping[str, JsonValue]) -> RuleResult:
        return RuleResult(self.id, RuleStatus.TRIGGERED, self.outcome, detail, evidence)

    def _passed(self, detail: str, evidence: Mapping[str, JsonValue]) -> RuleResult:
        return RuleResult(self.id, RuleStatus.PASSED, None, detail, evidence)

    def _not_applicable(self, detail: str) -> RuleResult:
        return RuleResult(self.id, RuleStatus.NOT_APPLICABLE, None, detail)


@dataclass(frozen=True, slots=True)
class AmountAbove(Rule):
    """Fires when the amount exceeds a per-currency limit."""

    id: str
    outcome: Outcome
    limits: Mapping[str, Money]

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        limit = self.limits.get(event.amount.currency.code)
        if limit is None:
            return self._not_applicable(f"no limit configured for {event.amount.currency.code}")
        evidence: dict[str, JsonValue] = {
            "amount": event.amount.format(),
            "limit": limit.format(),
            "currency": limit.currency.code,
        }
        if event.amount > limit:
            return self._triggered(f"amount {event.amount} exceeds {limit}", evidence)
        return self._passed(f"amount {event.amount} is within {limit}", evidence)


@dataclass(frozen=True, slots=True)
class MerchantCategory(Rule):
    """Fires for merchant category codes (ISO 18245) on a block list."""

    id: str
    outcome: Outcome
    categories: frozenset[str]

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        code = event.merchant.category_code
        evidence: dict[str, JsonValue] = {"category_code": code}
        if code in self.categories:
            return self._triggered(f"merchant category {code} is blocked", evidence)
        return self._passed(f"merchant category {code} is allowed", evidence)


@dataclass(frozen=True, slots=True)
class MerchantCountry(Rule):
    """Fires for merchants located in listed countries (e.g. comprehensive sanctions)."""

    id: str
    outcome: Outcome
    countries: frozenset[str]

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        country = event.merchant.country
        evidence: dict[str, JsonValue] = {"merchant_country": country}
        if country in self.countries:
            return self._triggered(f"merchant country {country} is blocked", evidence)
        return self._passed(f"merchant country {country} is allowed", evidence)


@dataclass(frozen=True, slots=True)
class AttemptVelocity(Rule):
    """Fires when the account already made ``max_prior_attempts`` attempts within the window."""

    id: str
    outcome: Outcome
    window_seconds: int
    max_prior_attempts: int

    @property
    def requires(self) -> FeatureRequest:
        return FeatureRequest(attempt_windows=frozenset({self.window_seconds}))

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        prior = features.attempts[self.window_seconds]
        evidence: dict[str, JsonValue] = {
            "prior_attempts": prior,
            "max_prior_attempts": self.max_prior_attempts,
            "window_seconds": self.window_seconds,
        }
        if prior >= self.max_prior_attempts:
            return self._triggered(
                f"{prior} prior attempts in {self.window_seconds}s (max {self.max_prior_attempts})",
                evidence,
            )
        return self._passed(f"{prior} prior attempts in {self.window_seconds}s", evidence)


@dataclass(frozen=True, slots=True)
class SpendVelocity(Rule):
    """Fires when approved spend in the window plus this amount would exceed the limit."""

    id: str
    outcome: Outcome
    window_seconds: int
    limits: Mapping[str, Money]

    @property
    def requires(self) -> FeatureRequest:
        return FeatureRequest(spend_windows=frozenset({self.window_seconds}))

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        limit = self.limits.get(event.amount.currency.code)
        if limit is None:
            return self._not_applicable(f"no limit configured for {event.amount.currency.code}")
        prior = Money(features.approved_spend[self.window_seconds], limit.currency)
        projected = prior + event.amount
        evidence: dict[str, JsonValue] = {
            "prior_approved_spend": prior.format(),
            "amount": event.amount.format(),
            "projected": projected.format(),
            "limit": limit.format(),
            "currency": limit.currency.code,
            "window_seconds": self.window_seconds,
        }
        if projected > limit:
            return self._triggered(
                f"spend in {self.window_seconds}s would reach {projected}, above {limit}", evidence
            )
        return self._passed(f"spend in {self.window_seconds}s would reach {projected}", evidence)


@dataclass(frozen=True, slots=True)
class NewMerchantAmount(Rule):
    """Fires when a first payment to a merchant the account has never used is large."""

    id: str
    outcome: Outcome
    limits: Mapping[str, Money]

    @property
    def requires(self) -> FeatureRequest:
        return FeatureRequest(merchant_history=True)

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        limit = self.limits.get(event.amount.currency.code)
        if limit is None:
            return self._not_applicable(f"no limit configured for {event.amount.currency.code}")
        seen = features.merchant_seen is True
        evidence: dict[str, JsonValue] = {
            "merchant_seen": seen,
            "amount": event.amount.format(),
            "limit": limit.format(),
            "currency": limit.currency.code,
        }
        if not seen and event.amount > limit:
            return self._triggered(
                f"first payment to {event.merchant.id} of {event.amount} exceeds {limit}", evidence
            )
        return self._passed("merchant already used or amount within limit", evidence)


@dataclass(frozen=True, slots=True)
class CrossBorder(Rule):
    """Fires when the merchant's country differs from the account's, on listed channels."""

    id: str
    outcome: Outcome
    channels: frozenset[Channel]

    def evaluate(self, event: TransactionEvent, features: Features) -> RuleResult:
        evidence: dict[str, JsonValue] = {
            "account_country": event.account_country,
            "merchant_country": event.merchant.country,
            "channel": event.channel.value,
        }
        if event.channel not in self.channels:
            return self._not_applicable(f"channel {event.channel.value} is not monitored")
        if event.merchant.country != event.account_country:
            return self._triggered(
                f"{event.channel.value} payment from {event.account_country} "
                f"to a merchant in {event.merchant.country}",
                evidence,
            )
        return self._passed("domestic payment", evidence)
