"""Reconstructable audit trail.

Every decision is stored with a self-contained record: the normalised event, the feature
values the rules saw, the ruleset version, each rule's verdict and evidence, the budget
state and stage timings. :func:`verify` rebuilds the decision from that record alone - it
re-parses the event, reloads the exact ruleset by content hash and re-runs the engine - and
checks three things:

1. the record still hashes to the digest written alongside it (no edits since),
2. the stored ruleset still hashes to the version the decision cites,
3. re-running the engine reproduces the same outcome, reasons and per-rule results.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sentinel import __version__
from sentinel.canonical import digest, format_timestamp
from sentinel.clock import StepBudget
from sentinel.domain import TransactionEvent, parse_transaction
from sentinel.errors import JsonValue, SentinelError
from sentinel.rules import EngineResult, Features, Ruleset, evaluate, load_ruleset
from sentinel.store.repository import DecisionRow

AUDIT_SCHEMA: Final = "sentinel.audit/v1"


def build_record(
    *,
    decision_id: UUID,
    event: TransactionEvent,
    fingerprint: str,
    ruleset: Ruleset,
    features: Features,
    result: EngineResult,
    budget_ms: int,
    feature_budget_ms: int,
    timings_us: Mapping[str, int],
    decided_at: datetime,
) -> dict[str, JsonValue]:
    return {
        "schema": AUDIT_SCHEMA,
        "engine_version": __version__,
        "decision_id": str(decision_id),
        "event": event.to_canonical(),
        "fingerprint": fingerprint,
        "ruleset": {"name": ruleset.name, "version": ruleset.version},
        "features": features.to_json(),
        "rules": [rule_result.to_json() for rule_result in result.results],
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "budget": {
            "budget_ms": budget_ms,
            "feature_budget_ms": feature_budget_ms,
            "exhausted": result.budget_exhausted,
            "rules_evaluated": result.rules_evaluated,
        },
        "timings_us": dict(timings_us),
        "decided_at": format_timestamp(decided_at),
    }


@dataclass(frozen=True, slots=True)
class Verification:
    decision_id: UUID
    digest_matches: bool
    ruleset_matches: bool
    replay_matches: bool
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.digest_matches and self.ruleset_matches and self.replay_matches

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "decision_id": str(self.decision_id),
            "ok": self.ok,
            "digest_matches": self.digest_matches,
            "ruleset_matches": self.ruleset_matches,
            "replay_matches": self.replay_matches,
            "problems": list(self.problems),
        }


def _replay(record: Mapping[str, Any], ruleset: Ruleset) -> EngineResult:
    event = parse_transaction(record["event"])
    features = Features.from_json(record["features"])
    budget = record["budget"]
    steps = budget["rules_evaluated"] if budget["exhausted"] else None
    return evaluate(event, features, ruleset, StepBudget(steps))


def verify(
    decision: DecisionRow,
    record: Mapping[str, Any],
    stored_digest: str,
    ruleset_config: object | None,
) -> Verification:
    problems: list[str] = []

    digest_matches = digest(record) == stored_digest
    if not digest_matches:
        problems.append("audit record does not match its digest: it was altered after writing")

    ruleset: Ruleset | None = None
    if ruleset_config is None:
        problems.append(f"ruleset {decision.ruleset_version} is missing")
    else:
        try:
            ruleset = load_ruleset(ruleset_config)
        except SentinelError as error:
            problems.append(f"stored ruleset no longer loads: {error.message}")
    ruleset_matches = ruleset is not None and ruleset.version == decision.ruleset_version
    if ruleset is not None and not ruleset_matches:
        problems.append("stored ruleset does not hash to the version the decision cites")

    replay_matches = False
    if ruleset_matches and ruleset is not None:
        try:
            replayed = _replay(record, ruleset)
        except (SentinelError, KeyError, TypeError, ValueError) as error:
            problems.append(f"record cannot be replayed: {error}")
        else:
            checks = {
                "outcome": replayed.outcome.value == record.get("outcome") == decision.outcome,
                "reasons": list(replayed.reasons) == record.get("reasons")
                and replayed.reasons == decision.reasons,
                "rule results": [r.to_json() for r in replayed.results] == record.get("rules"),
                "ruleset version": record.get("ruleset", {}).get("version")
                == decision.ruleset_version,
                "fingerprint": digest(record.get("event")) == decision.fingerprint,
            }
            mismatched = [name for name, matched in checks.items() if not matched]
            problems += [f"replay differs from the record: {name}" for name in mismatched]
            replay_matches = not mismatched

    return Verification(
        decision.decision_id, digest_matches, ruleset_matches, replay_matches, tuple(problems)
    )
