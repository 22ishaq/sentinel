"""Every decision can be rebuilt from its audit record, and tampering is detected."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from sentinel.canonical import digest
from sentinel.errors import ErrorCode, NotFoundError
from sentinel.service import DecisionService
from sentinel.store.db import Pool
from tests import support


def _decide(service: DecisionService, **overrides: object) -> UUID:
    return service.submit(support.event(**overrides)).decision.decision_id


def test_the_record_explains_every_rule(service: DecisionService) -> None:
    decision_id = _decide(service, amount="600.00")
    record = service.audit_report(decision_id).record
    assert record["schema"] == "sentinel.audit/v1"
    assert record["outcome"] == "REVIEW"
    assert record["reasons"] == ["high_value", "new_merchant"]
    assert [rule["rule_id"] for rule in record["rules"]] == [
        rule.id for rule in service.ruleset.rules
    ]
    high_value = next(rule for rule in record["rules"] if rule["rule_id"] == "high_value")
    assert high_value["evidence"] == {"amount": "600.00", "limit": "500.00", "currency": "GBP"}
    assert record["features"]["available"] is True
    assert set(record["timings_us"]) == {"validate", "claim", "features", "rules", "total"}


def test_a_fresh_decision_verifies(service: DecisionService) -> None:
    verification = service.audit_report(_decide(service)).verification
    assert verification.ok
    assert verification.problems == ()


def test_replays_use_the_features_captured_at_decision_time(service: DecisionService) -> None:
    first = _decide(service, amount="60.00")  # unknown merchant: reviewed
    later = _decide(service, amount="60.00")  # merchant now known: approved
    # Recomputing features today would show the merchant as known for both decisions.
    # The replay must use what the engine saw then, so both still verify.
    assert service.audit_report(first).record["features"]["merchant_seen"] is False
    assert service.audit_report(first).verification.ok
    assert service.audit_report(later).verification.ok


def test_editing_the_record_breaks_its_digest(service: DecisionService, pool: Pool) -> None:
    decision_id = _decide(service, amount="600.00")
    support.execute(
        pool,
        "UPDATE decision_audit SET record = jsonb_set(record, '{outcome}', '\"APPROVE\"')"
        " WHERE decision_id = %s",
        decision_id,
    )
    verification = service.audit_report(decision_id).verification
    assert not verification.digest_matches
    assert not verification.ok


def test_rewriting_record_and_digest_together_is_caught_by_replay(
    service: DecisionService, pool: Pool
) -> None:
    decision_id = _decide(service, amount="600.00")
    record = service.audit_report(decision_id).record
    forged = {**record, "outcome": "APPROVE", "reasons": []}
    support.execute(
        pool,
        "UPDATE decision_audit SET record = %s::jsonb, digest = %s WHERE decision_id = %s",
        support.json_text(forged),
        digest(forged),
        decision_id,
    )
    verification = service.audit_report(decision_id).verification
    assert verification.digest_matches
    assert not verification.replay_matches
    assert "replay differs from the record: outcome" in verification.problems


def test_editing_the_decision_row_is_caught_by_replay(service: DecisionService, pool: Pool) -> None:
    decision_id = _decide(service, amount="600.00")
    support.execute(pool, "UPDATE decisions SET outcome = 'APPROVE' WHERE id = %s", decision_id)
    assert not service.audit_report(decision_id).verification.replay_matches


def test_editing_the_stored_ruleset_is_detected(service: DecisionService, pool: Pool) -> None:
    decision_id = _decide(service)
    support.execute(
        pool,
        "UPDATE rulesets SET config = jsonb_set(config, '{rules,5,limits,GBP}', '\"5000.00\"')",
    )
    verification = service.audit_report(decision_id).verification
    assert not verification.ruleset_matches
    assert "stored ruleset does not hash to the version the decision cites" in verification.problems


def test_decisions_survive_a_ruleset_change(
    make_service: Callable[..., DecisionService],
) -> None:
    old = make_service(support.HIGH_VALUE)
    decision_id = _decide(old, amount="600.00")
    raised_limit = {**support.HIGH_VALUE, "limits": {"GBP": "1000.00"}}
    new = make_service(raised_limit)
    assert new.ruleset.version != old.ruleset.version
    assert _decide(new, amount="600.00") != decision_id
    report = new.audit_report(decision_id)
    assert report.decision.ruleset_version == old.ruleset.version
    assert report.verification.ok


def test_verify_all_walks_every_decision_in_batches(service: DecisionService) -> None:
    ids = {_decide(service) for _ in range(7)}
    verifications = list(service.verify_all(batch_size=3))
    assert {v.decision_id for v in verifications} == ids
    assert all(v.ok for v in verifications)


def test_audit_of_an_unknown_decision_is_not_found(service: DecisionService) -> None:
    with pytest.raises(NotFoundError) as caught:
        service.audit_report(uuid4())
    assert caught.value.code is ErrorCode.DECISION_NOT_FOUND
