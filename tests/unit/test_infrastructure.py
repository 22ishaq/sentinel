"""Errors, clock, metrics, settings, logging and outbox sinks."""

from __future__ import annotations

import http.server
import json
import logging
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from sentinel.clock import Deadline, StepBudget, SystemClock
from sentinel.config import Settings
from sentinel.errors import HTTP_STATUS, ConfigurationError, ErrorCode, MoneyError, preview
from sentinel.logs import JsonFormatter, configure_logging
from sentinel.metrics import Counter, Histogram, Metrics
from sentinel.outbox import OutboxMessage, WebhookSink
from tests.support import FakeClock


def test_every_error_code_maps_to_an_http_status() -> None:
    assert set(HTTP_STATUS) == set(ErrorCode)


def test_errors_serialise_and_gain_context_without_losing_their_class() -> None:
    error = MoneyError(ErrorCode.MONEY_MALFORMED, "bad", details={"amount": "x"})
    enriched = error.with_details(field="amount")
    assert isinstance(enriched, MoneyError)
    assert enriched.to_dict() == {
        "code": "MONEY_MALFORMED",
        "message": "bad",
        "details": {"amount": "x", "field": "amount"},
    }
    assert enriched.http_status == 422
    assert str(error) == "MONEY_MALFORMED: bad"


def test_preview_bounds_untrusted_values() -> None:
    assert preview("short") == "'short'"
    assert len(preview("x" * 1000)) == 64


def test_deadlines_count_down_on_the_monotonic_clock() -> None:
    clock = FakeClock()
    deadline = Deadline.after(clock, 10_000_000)
    assert deadline.remaining_ms() == 10
    clock.advance(9_500_000)
    assert deadline.remaining_ms() == 1  # rounded up, never zero while time remains
    assert not deadline.expired()
    clock.advance(500_000)
    assert deadline.expired()
    assert deadline.remaining_ms() == 1  # SQL timeouts must stay positive


def test_step_budgets_expire_after_a_fixed_number_of_checks() -> None:
    budget = StepBudget(2)
    assert [budget.expired() for _ in range(4)] == [False, False, True, True]
    assert not any(StepBudget(None).expired() for _ in range(100))


def test_the_system_clock_is_timezone_aware() -> None:
    clock = SystemClock()
    assert clock.now().tzinfo is not None
    assert clock.monotonic_ns() <= clock.monotonic_ns()


def test_counters_render_in_prometheus_format() -> None:
    counter = Counter("requests_total", "Requests.")
    counter.inc(labels={"route": 'a"b\\c\nd'})
    counter.inc(2, labels={"route": 'a"b\\c\nd'})
    assert counter.render() == [
        "# HELP requests_total Requests.",
        "# TYPE requests_total counter",
        'requests_total{route="a\\"b\\\\c\\nd"} 3',
    ]


def test_histograms_are_cumulative() -> None:
    histogram = Histogram("latency_seconds", "Latency.", [0.01, 0.1])
    for value in (0.005, 0.01, 0.05, 2.0):
        histogram.observe(value)
    lines = histogram.render()
    assert 'latency_seconds_bucket{le="0.01"} 2' in lines
    assert 'latency_seconds_bucket{le="0.1"} 3' in lines
    assert 'latency_seconds_bucket{le="+Inf"} 4' in lines
    assert "latency_seconds_count 4" in lines
    assert histogram.count == 4


def test_the_metric_set_renders_every_family() -> None:
    text = Metrics().render()
    for family in ("decisions_total", "decision_latency_seconds", "submit_latency_seconds"):
        assert f"# TYPE sentinel_{family}" in text


def test_settings_have_safe_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.database_url is None
    assert settings.host == "127.0.0.1"
    assert settings.budget_ms == 50
    assert settings.ruleset_path.name == "default.json"


def test_settings_read_the_environment() -> None:
    settings = Settings.from_env(
        {
            "SENTINEL_DATABASE_URL": "postgresql://db/sentinel",
            "SENTINEL_DECISION_BUDGET_MS": "25",
            "SENTINEL_RULESET": "/etc/sentinel/rules.json",
            "SENTINEL_LOG_LEVEL": "debug",
        }
    )
    assert settings.require_database_url() == "postgresql://db/sentinel"
    assert settings.budget_ms == 25
    assert settings.ruleset_path == Path("/etc/sentinel/rules.json")
    assert settings.log_level == "DEBUG"


@pytest.mark.parametrize(
    "environ",
    [
        {"SENTINEL_DECISION_BUDGET_MS": "fast"},
        {"SENTINEL_DECISION_BUDGET_MS": "0"},
        {"SENTINEL_POOL_MIN_SIZE": "10", "SENTINEL_POOL_MAX_SIZE": "5"},
    ],
)
def test_invalid_settings_fail_at_startup(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError) as caught:
        Settings.from_env(environ)
    assert caught.value.code is ErrorCode.SETTINGS_INVALID


def _explode() -> None:
    raise RuntimeError("boom")


def test_json_logs_include_extras_and_exceptions() -> None:
    record = logging.LogRecord(
        "sentinel", logging.WARNING, __file__, 1, "degraded %s", ("x",), None
    )
    record.outcome = "REVIEW"
    try:
        _explode()
    except RuntimeError:
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "degraded x"
    assert payload["outcome"] == "REVIEW"
    assert "RuntimeError: boom" in payload["exception"]


def test_configure_logging_installs_one_json_handler() -> None:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        configure_logging("WARNING")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


class _Receiver(http.server.BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, bytes]]] = []
    status: ClassVar[int] = 204

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        type(self).received.append((self.headers["Idempotency-Key"], self.rfile.read(length)))
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def receiver() -> Iterator[str]:
    _Receiver.received = []
    _Receiver.status = 204
    server = http.server.HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/hook"
    server.shutdown()
    server.server_close()


def test_the_webhook_sink_posts_canonical_envelopes(receiver: str) -> None:
    message = OutboxMessage(7, "decision.made", "acc_1", {"outcome": "APPROVE"}, 0)
    WebhookSink(receiver).publish(message)
    key, body = _Receiver.received[0]
    assert key == "sentinel-outbox-7"
    assert json.loads(body) == message.envelope()


def test_the_webhook_sink_raises_on_server_errors(receiver: str) -> None:
    _Receiver.status = 503
    with pytest.raises(OSError, match="503"):
        WebhookSink(receiver).publish(OutboxMessage(1, "t", "k", {}, 0))


def test_the_webhook_sink_only_speaks_http() -> None:
    with pytest.raises(ValueError, match="http"):
        WebhookSink("file:///etc/passwd")
