"""Transactional outbox relay: decision events out of PostgreSQL, at least once.

Decisions write their ``decision.made`` message into the ``outbox`` table in the same
transaction as the decision itself, so an event is published if and only if the decision
committed. The relay claims pending rows with ``FOR UPDATE SKIP LOCKED`` (several relays
never publish the same row concurrently), publishes them in id order, and marks them
published in the same transaction as the claim. A crash between publishing and committing
re-delivers the batch, so consumers deduplicate on the envelope ``id``.

A failing message stops its batch, preserving order behind it, and is retried on the next
pass. After ``max_attempts`` failures it is parked (left unpublished, with ``last_error``)
so one poison message cannot block the queue forever.
"""

from __future__ import annotations

import logging
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol, TextIO

import psycopg

from sentinel.canonical import canonical_json
from sentinel.errors import JsonValue
from sentinel.store.db import Pool
from sentinel.store.repository import OUTBOX_CHANNEL

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    id: int
    topic: str
    key: str
    payload: dict[str, Any]
    attempts: int

    def envelope(self) -> dict[str, JsonValue]:
        return {"id": self.id, "topic": self.topic, "key": self.key, "payload": self.payload}


class Sink(Protocol):
    def publish(self, message: OutboxMessage) -> None: ...


class StdoutSink:
    """Writes one canonical JSON envelope per line (pipe it into anything)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout

    def publish(self, message: OutboxMessage) -> None:
        self._stream.write(canonical_json(message.envelope()).decode("utf-8") + "\n")
        self._stream.flush()


class WebhookSink:
    """POSTs each envelope to a URL; any non-2xx response or network error is retried."""

    def __init__(self, url: str, *, timeout_s: float = 5.0) -> None:
        if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
            raise ValueError("webhook URL must be http(s)")
        self._url = url
        self._timeout_s = timeout_s

    def publish(self, message: OutboxMessage) -> None:
        request = urllib.request.Request(  # noqa: S310 - scheme checked in __init__
            self._url,
            data=canonical_json(message.envelope()),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": f"sentinel-outbox-{message.id}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s):  # noqa: S310
                pass
        except urllib.error.HTTPError as error:  # 4xx/5xx: close the body, report the status
            error.close()
            raise ConnectionError(f"webhook answered HTTP {error.code}") from None


class OutboxRelay:
    def __init__(
        self,
        pool: Pool,
        sink: Sink,
        *,
        batch_size: int = 100,
        max_attempts: int = 10,
    ) -> None:
        self._pool = pool
        self._sink = sink
        self.batch_size = batch_size
        self.max_attempts = max_attempts

    def run_once(self) -> int:
        """Publish one batch of pending messages; return how many were published."""
        published: list[int] = []
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                """
                SELECT id, topic, message_key, payload, attempts
                  FROM outbox
                 WHERE published_at IS NULL AND attempts < %s
                 ORDER BY id
                 LIMIT %s
                   FOR UPDATE SKIP LOCKED
                """,
                (self.max_attempts, self.batch_size),
            ).fetchall()
            for row in rows:
                message = OutboxMessage(*row)
                try:
                    self._sink.publish(message)
                except Exception as error:  # any sink failure is retried on a later pass
                    conn.execute(
                        "UPDATE outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                        (f"{type(error).__name__}: {error}"[:500], message.id),
                    )
                    log.warning(
                        "outbox publish failed",
                        extra={"outbox_id": message.id, "attempts": message.attempts + 1},
                    )
                    break
                published.append(message.id)
            if published:
                conn.execute(
                    "UPDATE outbox SET published_at = now(), attempts = attempts + 1,"
                    " last_error = NULL WHERE id = ANY(%s)",
                    (published,),
                )
        return len(published)

    def run_forever(
        self, conninfo: str, stop: threading.Event, *, poll_interval_s: float = 1.0
    ) -> None:
        """Drain, then sleep until NOTIFY (new decision) or the poll interval, until stopped."""
        with psycopg.connect(conninfo, autocommit=True) as listener:
            listener.execute(f"LISTEN {OUTBOX_CHANNEL}")
            while not stop.is_set():
                while self.run_once() == self.batch_size and not stop.is_set():
                    pass
                for _ in listener.notifies(timeout=poll_interval_s, stop_after=1):
                    pass
