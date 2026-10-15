"""The decision pipeline: validate, claim, gather features, evaluate, persist.

Everything after validation happens in one PostgreSQL transaction. The event row, the
decision, its audit record and the outbox message commit together or not at all, so a
client that retries after a timeout either gets the original decision back or finds no
trace of the first attempt - never half of one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final
from uuid import UUID, uuid4

import psycopg
from psycopg_pool import PoolTimeout

from sentinel.audit import Verification, build_record, verify
from sentinel.canonical import digest, format_timestamp
from sentinel.clock import Clock, Deadline, SystemClock
from sentinel.domain import TransactionEvent, parse_transaction
from sentinel.errors import (
    ConflictError,
    ErrorCode,
    JsonValue,
    NotFoundError,
    SentinelError,
    StoreUnavailableError,
)
from sentinel.metrics import Metrics
from sentinel.rules import (
    BUDGET_EXHAUSTED,
    FEATURES_UNAVAILABLE,
    EngineResult,
    Ruleset,
    evaluate,
)
from sentinel.store.db import Conn, Pool
from sentinel.store.repository import DecisionRow, Repository

DECISION_TOPIC: Final = "decision.made"

FEATURE_BUDGET_PERCENT: Final = 80
"""Share of the decision budget the lock + feature lookup may use. The rest is reserved so
the rules always run, and a static DECLINE (say, a blocked country) is never lost to a slow
feature query."""

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Submission:
    decision: DecisionRow
    replayed: bool


@dataclass(frozen=True, slots=True)
class AuditReport:
    decision: DecisionRow
    record: dict[str, Any]
    verification: Verification


def decision_message(
    decision: DecisionRow, event: TransactionEvent, result: EngineResult
) -> dict[str, JsonValue]:
    return {
        "type": DECISION_TOPIC,
        "decision_id": str(decision.decision_id),
        "event_id": decision.event_id,
        "account_id": decision.account_id,
        "outcome": result.outcome.value,
        "reasons": list(result.reasons),
        "amount": event.amount.to_json(),
        "ruleset_version": decision.ruleset_version,
        "decided_at": format_timestamp(decision.decided_at),
    }


class DecisionService:
    def __init__(
        self,
        pool: Pool,
        ruleset: Ruleset,
        *,
        budget_ms: int = 50,
        clock: Clock | None = None,
        repository: Repository | None = None,
        metrics: Metrics | None = None,
        max_future_skew: timedelta = timedelta(minutes=5),
    ) -> None:
        if budget_ms <= 0:
            raise ValueError("budget_ms must be positive")
        self.ruleset = ruleset
        self.budget_ms = budget_ms
        self.metrics = metrics or Metrics()
        self._pool = pool
        self._clock = clock or SystemClock()
        self._repo = repository or Repository()
        self._skew = max_future_skew

    @contextmanager
    def _connection(self) -> Iterator[Conn]:
        try:
            with self._pool.connection() as conn:
                yield conn
        except (PoolTimeout, psycopg.OperationalError) as error:
            raise StoreUnavailableError(
                ErrorCode.STORE_UNAVAILABLE,
                "the decision store is unavailable; retry with the same event_id",
                details={"reason": type(error).__name__},
            ) from error

    def register_ruleset(self) -> None:
        """Persist the active ruleset so every decision can cite (and replay) it."""
        with self._connection() as conn, conn.transaction():
            self._repo.save_ruleset(conn, self.ruleset)

    def ping(self) -> None:
        with self._connection() as conn:
            conn.execute("SELECT 1")

    def submit(self, raw: object) -> Submission:
        clock = self._clock
        started = clock.monotonic_ns()
        received_at = clock.now()
        budget_ns = self.budget_ms * 1_000_000
        deadline = Deadline.after(clock, budget_ns, start_ns=started)
        feature_deadline = Deadline.after(
            clock, budget_ns * FEATURE_BUDGET_PERCENT // 100, start_ns=started
        )
        try:
            event = parse_transaction(raw, now=received_at, max_future_skew=self._skew)
        except SentinelError as error:
            self.metrics.rejected.inc(labels={"code": error.code.value})
            raise
        fingerprint = event.fingerprint()
        marks = {"validate": clock.monotonic_ns()}

        with self._connection() as conn, conn.transaction():
            transaction_id = self._repo.claim(conn, event, fingerprint, received_at)
            if transaction_id is None:
                return self._replay(conn, event.event_id, fingerprint)
            marks["claim"] = clock.monotonic_ns()

            features = self._repo.load_features(
                conn, event, transaction_id, received_at, self.ruleset.requires, feature_deadline
            )
            marks["features"] = clock.monotonic_ns()
            result = evaluate(event, features, self.ruleset, deadline)
            marks["rules"] = clock.monotonic_ns()

            decision = DecisionRow(
                decision_id=uuid4(),
                transaction_id=transaction_id,
                event_id=event.event_id,
                account_id=event.account_id,
                fingerprint=fingerprint,
                outcome=result.outcome,
                reasons=result.reasons,
                ruleset_version=self.ruleset.version,
                budget_exhausted=result.budget_exhausted,
                decision_latency_us=(marks["rules"] - started) // 1_000,
                decided_at=clock.now(),
            )
            record = build_record(
                decision_id=decision.decision_id,
                event=event,
                fingerprint=fingerprint,
                ruleset=self.ruleset,
                features=features,
                result=result,
                budget_ms=self.budget_ms,
                feature_budget_ms=self.budget_ms * FEATURE_BUDGET_PERCENT // 100,
                timings_us=_stage_durations(started, marks),
                decided_at=decision.decided_at,
            )
            self._repo.insert_decision(
                conn,
                decision_id=decision.decision_id,
                transaction_id=transaction_id,
                result=result,
                ruleset_version=self.ruleset.version,
                latency_us=decision.decision_latency_us,
                decided_at=decision.decided_at,
            )
            self._repo.insert_audit(conn, decision.decision_id, record, digest(record))
            self._repo.enqueue(
                conn, DECISION_TOPIC, event.account_id, decision_message(decision, event, result)
            )

        self._observe(result, started, marks["rules"])
        return Submission(decision, replayed=False)

    def _replay(self, conn: Conn, event_id: str, fingerprint: str) -> Submission:
        existing = self._repo.decision_for_event(conn, event_id)
        if existing is None:  # pragma: no cover - the claim and its decision commit together
            raise SentinelError(ErrorCode.INTERNAL_ERROR, "claimed event has no decision")
        if existing.fingerprint != fingerprint:
            self.metrics.conflicts.inc()
            raise ConflictError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "event_id was already used for a different payload",
                details={"event_id": event_id, "decision_id": str(existing.decision_id)},
            )
        self.metrics.replays.inc()
        return Submission(existing, replayed=True)

    def _observe(self, result: EngineResult, started: int, decided: int) -> None:
        metrics = self.metrics
        metrics.decisions.inc(labels={"outcome": result.outcome.value})
        for reason in result.reasons:
            if reason in {BUDGET_EXHAUSTED, FEATURES_UNAVAILABLE}:
                metrics.degraded.inc(labels={"reason": reason})
        metrics.decision_latency.observe((decided - started) / 1e9)
        metrics.request_latency.observe((self._clock.monotonic_ns() - started) / 1e9)
        if result.degraded:
            log.warning(
                "degraded decision", extra={"outcome": result.outcome, "reasons": result.reasons}
            )

    def decision(self, decision_id: UUID) -> DecisionRow:
        with self._connection() as conn:
            found = self._repo.decision(conn, decision_id)
        if found is None:
            raise NotFoundError(
                ErrorCode.DECISION_NOT_FOUND,
                "no decision with that id",
                details={"decision_id": str(decision_id)},
            )
        return found

    def audit_report(self, decision_id: UUID) -> AuditReport:
        with self._connection() as conn:
            found = self._repo.decision(conn, decision_id)
            audit = self._repo.audit(conn, decision_id)
        if found is None or audit is None:
            raise NotFoundError(
                ErrorCode.DECISION_NOT_FOUND,
                "no decision with that id",
                details={"decision_id": str(decision_id)},
            )
        return AuditReport(
            found, audit.record, verify(found, audit.record, audit.digest, audit.ruleset_config)
        )

    def verify_all(self, *, batch_size: int = 500) -> Iterator[Verification]:
        """Re-derive every stored decision from its audit record, in batches."""
        after: UUID | None = None
        while True:
            with self._connection() as conn:
                ids = self._repo.decision_ids(conn, after=after, limit=batch_size)
            if not ids:
                return
            for decision_id in ids:
                yield self.audit_report(decision_id).verification
            after = ids[-1]


def _stage_durations(started: int, marks: dict[str, int]) -> dict[str, int]:
    durations: dict[str, int] = {}
    previous = started
    for stage, at in marks.items():
        durations[stage] = (at - previous) // 1_000
        previous = at
    durations["total"] = (previous - started) // 1_000
    return durations
