"""The latency budget, enforced inside PostgreSQL and in the engine."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import psycopg

from sentinel.domain import Outcome
from sentinel.rules import BUDGET_EXHAUSTED, FEATURES_UNAVAILABLE
from sentinel.service import DecisionService
from sentinel.store.db import Pool
from tests import support


@contextmanager
def account_locked(url: str, account_id: str) -> Iterator[None]:
    """Hold the per-account advisory lock from another session, like a slow decision would."""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (account_id,))
        yield


def test_lock_contention_beyond_the_budget_falls_back_to_review(
    make_service: Callable[..., DecisionService], pool: Pool, migrated_url: str
) -> None:
    service = make_service(support.DAILY_SPEND, budget_ms=40)
    started = time.perf_counter()
    with account_locked(migrated_url, "acc_alice"):
        decision = service.submit(support.event()).decision
    elapsed = time.perf_counter() - started
    assert decision.outcome is Outcome.REVIEW
    assert decision.reasons == (FEATURES_UNAVAILABLE,)
    assert elapsed < 1.0  # waited ~40 ms for the lock, not indefinitely
    assert support.count(pool, "decisions") == 1  # the event was still recorded
    assert service.metrics.degraded.value({"reason": FEATURES_UNAVAILABLE}) == 1


def test_rules_that_need_no_history_still_decide_when_features_time_out(
    make_service: Callable[..., DecisionService], migrated_url: str
) -> None:
    service = make_service(support.BLOCKED_COUNTRY, support.DAILY_SPEND, budget_ms=40)
    with account_locked(migrated_url, "acc_alice"):
        decision = service.submit(support.event(merchant=support.merchant(country="KP"))).decision
    # A DECLINE that fired is never softened to the REVIEW fallback.
    assert decision.outcome is Outcome.DECLINE
    assert decision.reasons == ("blocked_country", FEATURES_UNAVAILABLE)


def test_a_fail_open_ruleset_approves_when_evidence_is_missing(
    make_service: Callable[..., DecisionService], migrated_url: str
) -> None:
    fail_open = support.ruleset(support.DAILY_SPEND, fallback="APPROVE")
    service = make_service(ruleset=fail_open, budget_ms=40)
    with account_locked(migrated_url, "acc_alice"):
        decision = service.submit(support.event()).decision
    assert decision.outcome is Outcome.APPROVE
    assert decision.reasons == (FEATURES_UNAVAILABLE,)


def test_a_slow_feature_query_is_cancelled_at_the_budget(
    make_service: Callable[..., DecisionService], pool: Pool, migrated_url: str
) -> None:
    # An ACCESS EXCLUSIVE lock on decisions blocks the spend query (and later the insert).
    # statement_timeout cancels the query at the budget; once the lock is released the
    # decision is persisted as a fallback.
    service = make_service(support.DAILY_SPEND, budget_ms=60)
    locked, release = threading.Event(), threading.Event()

    def hold_lock() -> None:
        with psycopg.connect(migrated_url) as conn, conn.transaction():
            conn.execute("LOCK TABLE decisions IN ACCESS EXCLUSIVE MODE")
            locked.set()
            release.wait(5)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    locked.wait(5)
    threading.Timer(0.4, release.set).start()
    decision = service.submit(support.event()).decision
    holder.join()
    assert decision.outcome is Outcome.REVIEW
    assert decision.reasons == (FEATURES_UNAVAILABLE,)
    audit = service.audit_report(decision.decision_id)
    assert "cancelled" in audit.record["features"]["error"]
    assert audit.verification.ok


def test_rulesets_without_history_rules_never_wait_for_the_account_lock(
    make_service: Callable[..., DecisionService], migrated_url: str
) -> None:
    service = make_service(support.BLOCKED_COUNTRY, support.CEILING, budget_ms=40)
    with account_locked(migrated_url, "acc_alice"):
        decision = service.submit(support.event()).decision
    assert decision.outcome is Outcome.APPROVE
    assert decision.reasons == ()


def test_an_exhausted_budget_skips_the_remaining_rules(
    make_service: Callable[..., DecisionService],
) -> None:
    # Every clock read costs 30 ms against a 50 ms budget: it runs out before any rule.
    service = make_service(
        support.CEILING,
        support.HIGH_VALUE,
        budget_ms=50,
        clock=support.FakeClock(step_ns=30_000_000),
    )
    decision = service.submit(support.event()).decision
    assert decision.outcome is Outcome.REVIEW
    assert decision.budget_exhausted is True
    assert decision.reasons == (BUDGET_EXHAUSTED,)
    record = service.audit_report(decision.decision_id).record
    assert {rule["status"] for rule in record["rules"]} == {"skipped"}
    assert record["budget"] == {
        "budget_ms": 50,
        "feature_budget_ms": 40,
        "exhausted": True,
        "rules_evaluated": 0,
    }
