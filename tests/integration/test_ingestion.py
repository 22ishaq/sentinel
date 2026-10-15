"""Duplicate-safe ingestion against a real PostgreSQL."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from sentinel.canonical import format_timestamp
from sentinel.domain import Outcome
from sentinel.errors import (
    ConflictError,
    ErrorCode,
    MoneyError,
    NotFoundError,
    SentinelError,
    StoreUnavailableError,
    ValidationError,
)
from sentinel.service import DecisionService, Submission
from sentinel.store.db import Pool, create_pool
from tests import support

TABLES = ("transactions", "decisions", "decision_audit", "outbox")


def _counts(pool: Pool) -> dict[str, int]:
    return {table: support.count(pool, table) for table in TABLES}


def test_a_new_event_commits_event_decision_audit_and_outbox_together(
    service: DecisionService, pool: Pool
) -> None:
    submission = service.submit(support.event())
    assert submission.replayed is False
    assert submission.decision.outcome is Outcome.APPROVE
    assert submission.decision.reasons == ()
    assert _counts(pool) == dict.fromkeys(TABLES, 1)


def test_the_decision_row_records_ruleset_and_latency(service: DecisionService, pool: Pool) -> None:
    decision = service.submit(support.event()).decision
    stored = service.decision(decision.decision_id)
    assert stored == decision
    assert stored.ruleset_version == service.ruleset.version
    assert 0 <= stored.decision_latency_us < 5_000_000
    assert stored.budget_exhausted is False


def test_amounts_are_stored_as_exact_minor_units(
    make_service: Callable[..., DecisionService], pool: Pool
) -> None:
    service = make_service(support.BLOCKED_COUNTRY)
    wide = "1234567890123456789012345678901234567"  # 37 digits of JPY: beyond float and int64
    service.submit(support.event(amount=wide, currency="JPY"))
    stored = support.scalar(pool, "SELECT amount_minor FROM transactions")
    assert stored == Decimal(wide)


def test_a_retry_with_the_same_payload_returns_the_original_decision(
    service: DecisionService, pool: Pool
) -> None:
    payload = support.event()
    first = service.submit(payload)
    second = service.submit(payload)
    assert second.replayed is True
    assert second.decision == first.decision
    assert _counts(pool) == dict.fromkeys(TABLES, 1)


def test_equivalent_payloads_are_recognised_as_the_same_event(service: DecisionService) -> None:
    payload = support.event(amount="25.00")
    first = service.submit(payload)
    reordered = dict(reversed(list(payload.items())))
    reordered["amount"] = "25.0"
    reordered["occurred_at"] = payload["occurred_at"].replace("Z", "+00:00")
    assert service.submit(reordered).decision.decision_id == first.decision.decision_id


def test_reusing_an_event_id_for_a_different_payload_is_a_conflict(
    service: DecisionService, pool: Pool
) -> None:
    payload = support.event()
    original = service.submit(payload)
    with pytest.raises(ConflictError) as caught:
        service.submit({**payload, "amount": "26.00"})
    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    assert caught.value.details["decision_id"] == str(original.decision.decision_id)
    assert _counts(pool) == dict.fromkeys(TABLES, 1)
    assert service.metrics.conflicts.value() == 1


def test_concurrent_duplicates_produce_exactly_one_decision(
    service: DecisionService, pool: Pool
) -> None:
    payload = support.event()
    with ThreadPoolExecutor(max_workers=16) as executor:
        submissions = list(executor.map(lambda _: service.submit(payload), range(32)))
    assert {s.decision.decision_id for s in submissions} == {submissions[0].decision.decision_id}
    assert sum(not s.replayed for s in submissions) == 1
    assert _counts(pool) == dict.fromkeys(TABLES, 1)


def test_concurrent_conflicting_payloads_let_exactly_one_win(
    service: DecisionService, pool: Pool
) -> None:
    event_id = f"evt_{uuid4().hex}"

    def attempt(index: int) -> Submission | ErrorCode:
        try:
            return service.submit(support.event(event_id=event_id, amount=f"{10 + index}.00"))
        except SentinelError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(attempt, range(12)))
    winners = [r for r in results if isinstance(r, Submission) and not r.replayed]
    assert len(winners) == 1
    assert all(
        r is ErrorCode.IDEMPOTENCY_CONFLICT for r in results if not isinstance(r, Submission)
    )
    assert support.count(pool, "decisions") == 1


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"amount": 25.0}, ErrorCode.MONEY_FLOAT_REJECTED),
        ({"amount": "25.001"}, ErrorCode.MONEY_PRECISION_EXCEEDED),
        ({"currency": "XAU"}, ErrorCode.CURRENCY_NOT_TRANSACTABLE),
        ({"amount": "0.00"}, ErrorCode.EVENT_AMOUNT_NOT_POSITIVE),
        ({"channel": "carrier_pigeon"}, ErrorCode.EVENT_FIELD_INVALID),
    ],
)
def test_invalid_events_are_rejected_before_anything_is_written(
    service: DecisionService, pool: Pool, overrides: dict[str, object], code: ErrorCode
) -> None:
    with pytest.raises(SentinelError) as caught:
        service.submit(support.event(**overrides))
    assert caught.value.code is code
    assert _counts(pool) == dict.fromkeys(TABLES, 0)
    assert service.metrics.rejected.value({"code": code.value}) == 1


def test_money_errors_name_the_offending_field(service: DecisionService) -> None:
    with pytest.raises(MoneyError) as caught:
        service.submit(support.event(currency="gbp"))
    assert caught.value.details["field"] == "currency"


def test_events_from_the_future_are_rejected(service: DecisionService) -> None:
    future = format_timestamp(datetime.now(UTC) + timedelta(hours=1))
    with pytest.raises(ValidationError) as caught:
        service.submit(support.event(occurred_at=future))
    assert caught.value.code is ErrorCode.EVENT_TIMESTAMP_IN_FUTURE


def test_the_outbox_message_describes_the_decision(service: DecisionService, pool: Pool) -> None:
    decision = service.submit(support.event(amount="12.50")).decision
    topic, key, payload = support.scalar(
        pool, "SELECT ARRAY[topic, message_key, payload::text] FROM outbox"
    )
    assert (topic, key) == ("decision.made", "acc_alice")
    assert f'"decision_id": "{decision.decision_id}"' in payload
    assert '"amount": {"amount": "12.50", "currency": "GBP"}' in payload


def test_unknown_decisions_are_not_found(service: DecisionService) -> None:
    with pytest.raises(NotFoundError) as caught:
        service.decision(uuid4())
    assert caught.value.code is ErrorCode.DECISION_NOT_FOUND


def test_metrics_count_outcomes_and_replays(service: DecisionService) -> None:
    payload = support.event()
    service.submit(payload)
    service.submit(payload)
    service.submit(support.event(merchant=support.merchant(country="KP")))
    assert service.metrics.decisions.value({"outcome": "APPROVE"}) == 1
    assert service.metrics.decisions.value({"outcome": "DECLINE"}) == 1
    assert service.metrics.replays.value() == 1
    assert service.metrics.decision_latency.count == 2


def test_an_unreachable_database_is_reported_as_store_unavailable(migrated_url: str) -> None:
    pool = create_pool(migrated_url, min_size=1, max_size=1)
    service = DecisionService(pool, support.ruleset(support.CEILING))
    pool.close()
    with pytest.raises(StoreUnavailableError) as caught:
        service.submit(support.event())
    assert caught.value.code is ErrorCode.STORE_UNAVAILABLE
    assert caught.value.http_status == 503
