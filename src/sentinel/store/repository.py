"""All SQL lives here. Methods take a connection so callers own transaction boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import errors, sql
from psycopg.types.json import Jsonb

from sentinel.canonical import format_timestamp
from sentinel.clock import Deadline
from sentinel.domain import Outcome, TransactionEvent
from sentinel.errors import JsonValue
from sentinel.rules import EngineResult, FeatureRequest, Features, Ruleset
from sentinel.store.db import Conn

OUTBOX_CHANNEL: Final = "sentinel_outbox"


@dataclass(frozen=True, slots=True)
class DecisionRow:
    decision_id: UUID
    transaction_id: int
    event_id: str
    account_id: str
    fingerprint: str
    outcome: Outcome
    reasons: tuple[str, ...]
    ruleset_version: str
    budget_exhausted: bool
    decision_latency_us: int
    decided_at: datetime

    def to_json(self) -> dict[str, JsonValue]:
        return {
            "decision_id": str(self.decision_id),
            "event_id": self.event_id,
            "account_id": self.account_id,
            "outcome": self.outcome.value,
            "reasons": list(self.reasons),
            "ruleset_version": self.ruleset_version,
            "budget_exhausted": self.budget_exhausted,
            "decision_latency_us": self.decision_latency_us,
            "decided_at": format_timestamp(self.decided_at),
        }


@dataclass(frozen=True, slots=True)
class AuditRow:
    record: dict[str, Any]
    digest: str
    ruleset_config: dict[str, Any] | None


_SELECT_DECISION: Final = """
    SELECT d.id, t.id, t.event_id, t.account_id, t.fingerprint, d.outcome, d.reasons,
           d.ruleset_version, d.budget_exhausted, d.decision_latency_us, d.decided_at
      FROM decisions d
      JOIN transactions t ON t.id = d.transaction_id
"""


def _decision(row: tuple[Any, ...]) -> DecisionRow:
    return DecisionRow(
        decision_id=row[0],
        transaction_id=row[1],
        event_id=row[2],
        account_id=row[3],
        fingerprint=row[4],
        outcome=Outcome(row[5]),
        reasons=tuple(row[6]),
        ruleset_version=row[7],
        budget_exhausted=row[8],
        decision_latency_us=row[9],
        decided_at=row[10],
    )


class Repository:
    def save_ruleset(self, conn: Conn, ruleset: Ruleset) -> None:
        conn.execute(
            "INSERT INTO rulesets (version, name, config) VALUES (%s, %s, %s)"
            " ON CONFLICT (version) DO NOTHING",
            (ruleset.version, ruleset.name, Jsonb(ruleset.config)),
        )

    def claim(
        self, conn: Conn, event: TransactionEvent, fingerprint: str, received_at: datetime
    ) -> int | None:
        """Insert the event unless its ``event_id`` exists. Returns the new row id, or None.

        A concurrent writer of the same ``event_id`` makes this statement wait until that
        writer commits (then None: read its decision) or rolls back (then we own the event).
        """
        row = conn.execute(
            """
            INSERT INTO transactions (event_id, fingerprint, account_id, occurred_at,
                                      received_at, amount_minor, currency, merchant_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            RETURNING id
            """,
            (
                event.event_id,
                fingerprint,
                event.account_id,
                event.occurred_at,
                received_at,
                event.amount.minor_units,
                event.amount.currency.code,
                event.merchant.id,
                Jsonb(event.to_canonical()),
            ),
        ).fetchone()
        return None if row is None else int(row[0])

    def decision_for_event(self, conn: Conn, event_id: str) -> DecisionRow | None:
        row = conn.execute(_SELECT_DECISION + " WHERE t.event_id = %s", (event_id,)).fetchone()
        return None if row is None else _decision(row)

    def decision(self, conn: Conn, decision_id: UUID) -> DecisionRow | None:
        row = conn.execute(_SELECT_DECISION + " WHERE d.id = %s", (decision_id,)).fetchone()
        return None if row is None else _decision(row)

    def decision_ids(self, conn: Conn, *, after: UUID | None, limit: int) -> list[UUID]:
        rows = conn.execute(
            "SELECT id FROM decisions WHERE %s::uuid IS NULL OR id > %s ORDER BY id LIMIT %s",
            (after, after, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def features_query(self, request: FeatureRequest) -> sql.Composed:
        """One round trip for every feature the ruleset needs."""
        columns: list[sql.Composable] = [
            sql.SQL(
                "(SELECT count(*) FROM transactions"
                " WHERE account_id = %(account)s AND id <> %(self)s"
                " AND received_at > %(received_at)s - make_interval(secs => {}))"
            ).format(sql.Literal(window))
            for window in sorted(request.attempt_windows)
        ]
        columns += [
            sql.SQL(
                "(SELECT coalesce(sum(t.amount_minor), 0) FROM transactions t"
                " JOIN decisions d ON d.transaction_id = t.id"
                " WHERE t.account_id = %(account)s AND t.currency = %(currency)s"
                " AND d.outcome = 'APPROVE'"
                " AND t.received_at > %(received_at)s - make_interval(secs => {}))"
            ).format(sql.Literal(window))
            for window in sorted(request.spend_windows)
        ]
        if request.merchant_history:
            columns.append(
                sql.SQL(
                    "EXISTS (SELECT 1 FROM transactions WHERE account_id = %(account)s"
                    " AND merchant_id = %(merchant)s AND id <> %(self)s)"
                )
            )
        return sql.SQL("SELECT {}").format(sql.SQL(", ").join(columns))

    def load_features(
        self,
        conn: Conn,
        event: TransactionEvent,
        transaction_id: int,
        received_at: datetime,
        request: FeatureRequest,
        deadline: Deadline,
    ) -> Features:
        """Lock the account and read its history, within whatever is left of the budget.

        The per-account advisory lock serialises decisions for one account, so two
        concurrent payments cannot both pass a spend limit that only one of them fits in.
        Both the lock wait and the query are capped at the remaining budget; if either runs
        out the savepoint is rolled back and the features are reported unavailable.
        """
        if request.empty:
            return Features()
        if deadline.expired():
            return Features.unavailable("budget exhausted before the feature lookup")
        params: dict[str, object] = {
            "account": event.account_id,
            "self": transaction_id,
            "received_at": received_at,
            "currency": event.amount.currency.code,
            "merchant": event.merchant.id,
        }
        try:
            with conn.transaction():  # savepoint: a cancelled lookup must not abort the claim
                limit = f"{deadline.remaining_ms()}ms"
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, true),"
                    " set_config('statement_timeout', %s, true)",
                    (limit, limit),
                )
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (event.account_id,)
                )
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{deadline.remaining_ms()}ms",),
                )
                row = conn.execute(self.features_query(request), params).fetchone()
                conn.execute(
                    "SET LOCAL statement_timeout TO DEFAULT; SET LOCAL lock_timeout TO DEFAULT"
                )
        except errors.LockNotAvailable:
            return Features.unavailable("account lock not acquired within the decision budget")
        except errors.QueryCanceled:
            return Features.unavailable("feature query cancelled at the decision budget")
        if row is None:  # pragma: no cover - a scalar SELECT always returns one row
            return Features.unavailable("feature query returned no row")

        values = iter(row)
        attempts = {window: int(next(values)) for window in sorted(request.attempt_windows)}
        spend = {window: int(next(values)) for window in sorted(request.spend_windows)}
        seen = bool(next(values)) if request.merchant_history else None
        return Features(attempts=attempts, approved_spend=spend, merchant_seen=seen)

    def insert_decision(
        self,
        conn: Conn,
        *,
        decision_id: UUID,
        transaction_id: int,
        result: EngineResult,
        ruleset_version: str,
        latency_us: int,
        decided_at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO decisions (id, transaction_id, outcome, reasons, ruleset_version,
                                   budget_exhausted, decision_latency_us, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                decision_id,
                transaction_id,
                result.outcome.value,
                list(result.reasons),
                ruleset_version,
                result.budget_exhausted,
                latency_us,
                decided_at,
            ),
        )

    def insert_audit(
        self, conn: Conn, decision_id: UUID, record: Mapping[str, object], digest: str
    ) -> None:
        conn.execute(
            "INSERT INTO decision_audit (decision_id, record, digest) VALUES (%s, %s, %s)",
            (decision_id, Jsonb(record), digest),
        )

    def audit(self, conn: Conn, decision_id: UUID) -> AuditRow | None:
        row = conn.execute(
            """
            SELECT a.record, a.digest, r.config
              FROM decision_audit a
              JOIN decisions d ON d.id = a.decision_id
              LEFT JOIN rulesets r ON r.version = d.ruleset_version
             WHERE a.decision_id = %s
            """,
            (decision_id,),
        ).fetchone()
        return (
            None if row is None else AuditRow(record=row[0], digest=row[1], ruleset_config=row[2])
        )

    def enqueue(self, conn: Conn, topic: str, key: str, payload: Mapping[str, object]) -> None:
        """Write an outbox message; NOTIFY wakes the relay when this transaction commits."""
        conn.execute(
            "INSERT INTO outbox (topic, message_key, payload) VALUES (%s, %s, %s)",
            (topic, key, Jsonb(payload)),
        )
        conn.execute("SELECT pg_notify(%s, '')", (OUTBOX_CHANNEL,))
