"""The transactional outbox relay against real PostgreSQL."""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable

from sentinel.outbox import OutboxMessage, OutboxRelay, StdoutSink
from sentinel.service import DecisionService
from sentinel.store.db import Pool
from tests import support


class RecordingSink:
    def __init__(self, fail_ids: set[int] | None = None, delay_s: float = 0.0) -> None:
        self.messages: list[OutboxMessage] = []
        self.fail_ids = fail_ids or set()
        self.delay_s = delay_s
        self._lock = threading.Lock()

    def publish(self, message: OutboxMessage) -> None:
        time.sleep(self.delay_s)
        if message.id in self.fail_ids:
            raise ConnectionError("downstream unavailable")
        with self._lock:
            self.messages.append(message)

    @property
    def ids(self) -> list[int]:
        return [message.id for message in self.messages]


def _decide(service: DecisionService, n: int) -> None:
    for _ in range(n):
        service.submit(support.event())


def test_pending_messages_are_published_in_order_and_marked(
    service: DecisionService, pool: Pool
) -> None:
    _decide(service, 3)
    sink = RecordingSink()
    assert OutboxRelay(pool, sink).run_once() == 3
    assert sink.ids == [1, 2, 3]
    assert support.count(pool, "outbox", "published_at IS NULL") == 0
    assert {m.topic for m in sink.messages} == {"decision.made"}


def test_published_messages_are_not_sent_again(service: DecisionService, pool: Pool) -> None:
    _decide(service, 2)
    sink = RecordingSink()
    relay = OutboxRelay(pool, sink)
    relay.run_once()
    assert relay.run_once() == 0
    assert sink.ids == [1, 2]


def test_a_failure_stops_the_batch_to_preserve_order(service: DecisionService, pool: Pool) -> None:
    _decide(service, 3)
    sink = RecordingSink(fail_ids={2})
    assert OutboxRelay(pool, sink).run_once() == 1
    assert sink.ids == [1]
    attempts, error = support.scalar(
        pool, "SELECT ARRAY[attempts::text, last_error] FROM outbox WHERE id = 2"
    )
    assert attempts == "1"
    assert "ConnectionError: downstream unavailable" in error
    assert support.count(pool, "outbox", "published_at IS NULL") == 2


def test_failed_messages_are_retried_first_once_the_sink_recovers(
    service: DecisionService, pool: Pool
) -> None:
    _decide(service, 3)
    sink = RecordingSink(fail_ids={2})
    relay = OutboxRelay(pool, sink)
    relay.run_once()
    sink.fail_ids.clear()
    assert relay.run_once() == 2
    assert sink.ids == [1, 2, 3]
    assert support.count(pool, "outbox", "last_error IS NOT NULL") == 0


def test_a_poison_message_is_parked_after_max_attempts(
    service: DecisionService, pool: Pool
) -> None:
    _decide(service, 2)
    sink = RecordingSink(fail_ids={1})
    relay = OutboxRelay(pool, sink, max_attempts=3)
    for _ in range(3):
        assert relay.run_once() == 0
    assert relay.run_once() == 1
    assert sink.ids == [2]
    assert support.scalar(pool, "SELECT attempts FROM outbox WHERE id = 1") == 3


def test_concurrent_relays_never_publish_a_message_twice(
    service: DecisionService, pool: Pool
) -> None:
    _decide(service, 12)
    sink = RecordingSink(delay_s=0.01)
    relays = [OutboxRelay(pool, sink, batch_size=4) for _ in range(3)]

    def drain(relay: OutboxRelay) -> None:
        while relay.run_once():
            pass

    threads = [threading.Thread(target=drain, args=(relay,)) for relay in relays]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(sink.ids) == list(range(1, 13))
    assert len(sink.ids) == 12


def test_run_forever_wakes_on_notify_and_stops_cleanly(
    make_service: Callable[..., DecisionService], pool: Pool, migrated_url: str
) -> None:
    service = make_service(support.CEILING)
    sink = RecordingSink()
    stop = threading.Event()
    relay = OutboxRelay(pool, sink)
    worker = threading.Thread(
        target=relay.run_forever, args=(migrated_url, stop), kwargs={"poll_interval_s": 5.0}
    )
    worker.start()
    try:
        time.sleep(0.2)  # let the relay LISTEN and go idle
        started = time.perf_counter()
        service.submit(support.event())
        deadline = started + 3.0
        while not sink.messages and time.perf_counter() < deadline:
            time.sleep(0.01)
        latency = time.perf_counter() - started
    finally:
        stop.set()
        worker.join(timeout=10)
    assert sink.ids == [1]
    assert latency < 2.0  # woken by NOTIFY, not the 5 s poll
    assert not worker.is_alive()


def test_stdout_sink_writes_canonical_envelopes(service: DecisionService, pool: Pool) -> None:
    decision = service.submit(support.event()).decision
    stream = io.StringIO()
    OutboxRelay(pool, StdoutSink(stream)).run_once()
    envelope = json.loads(stream.getvalue())
    assert envelope["id"] == 1
    assert envelope["key"] == "acc_alice"
    assert envelope["payload"]["decision_id"] == str(decision.decision_id)
    assert stream.getvalue().count("\n") == 1
