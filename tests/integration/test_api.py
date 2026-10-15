"""The HTTP API, end to end through Starlette into PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from sentinel.api import MAX_BODY_BYTES, app_from_env, create_app
from sentinel.service import DecisionService
from sentinel.store.db import Pool, create_pool
from tests import support


@pytest.fixture
def client(service: DecisionService) -> Iterator[TestClient]:
    with TestClient(create_app(service)) as test_client:
        yield test_client


def _post(client: TestClient, payload: Any) -> Any:
    return client.post("/v1/transactions", json=payload)


def _error_code(response: Any) -> str:
    body = response.json()
    assert set(body["error"]) == {"code", "message", "details"}
    return str(body["error"]["code"])


def test_a_new_event_is_created(client: TestClient) -> None:
    response = _post(client, support.event())
    assert response.status_code == 201
    body = response.json()
    assert body["outcome"] == "APPROVE"
    assert body["replayed"] is False
    assert response.headers["location"] == f"/v1/decisions/{body['decision_id']}"
    assert "idempotent-replayed" not in response.headers


def test_a_retry_is_answered_from_the_store(client: TestClient) -> None:
    payload = support.event()
    first = _post(client, payload)
    second = _post(client, payload)
    assert second.status_code == 200
    assert second.headers["idempotent-replayed"] == "true"
    assert second.json()["decision_id"] == first.json()["decision_id"]
    assert second.json()["replayed"] is True


def test_a_reused_event_id_is_a_409(client: TestClient) -> None:
    payload = support.event()
    _post(client, payload)
    response = _post(client, {**payload, "amount": "99.99"})
    assert response.status_code == 409
    assert _error_code(response) == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    ("overrides", "status", "code", "field"),
    [
        ({"amount": 25.5}, 422, "MONEY_FLOAT_REJECTED", "amount"),
        ({"amount": 25}, 201, None, None),
        ({"amount": "25.555"}, 422, "MONEY_PRECISION_EXCEEDED", "amount"),
        ({"amount": "1e3"}, 422, "MONEY_MALFORMED", "amount"),
        ({"amount": "-5.00"}, 422, "EVENT_AMOUNT_NOT_POSITIVE", "amount"),
        ({"currency": "ZZZ"}, 422, "CURRENCY_UNKNOWN", "currency"),
        ({"currency": "gbp"}, 422, "CURRENCY_MALFORMED", "currency"),
        ({"currency": "XAU"}, 422, "CURRENCY_NOT_TRANSACTABLE", "currency"),
        ({"occurred_at": "2026-10-15 09:30:00"}, 422, "EVENT_FIELD_INVALID", "occurred_at"),
        ({"event_id": ""}, 422, "EVENT_FIELD_INVALID", "event_id"),
        ({"channel": "fax"}, 422, "EVENT_FIELD_INVALID", "channel"),
        (
            {"merchant": {"id": "m", "category_code": "54", "country": "GB"}},
            422,
            "EVENT_FIELD_INVALID",
            "merchant.category_code",
        ),
    ],
)
def test_validation_errors_carry_codes_and_fields(
    client: TestClient,
    overrides: dict[str, Any],
    status: int,
    code: str | None,
    field: str | None,
) -> None:
    response = _post(client, support.event(**overrides))
    assert response.status_code == status
    if code is not None:
        assert _error_code(response) == code
        assert response.json()["error"]["details"]["field"] == field


def test_missing_fields_are_named(client: TestClient) -> None:
    payload = support.event()
    del payload["merchant"]
    response = _post(client, payload)
    assert response.status_code == 422
    assert _error_code(response) == "EVENT_FIELD_MISSING"
    assert response.json()["error"]["details"]["field"] == "merchant"


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    response = _post(client, support.event(risk_override="APPROVE"))
    assert response.status_code == 422
    assert _error_code(response) == "EVENT_FIELD_UNKNOWN"
    assert response.json()["error"]["details"]["fields"] == ["risk_override"]


@pytest.mark.parametrize(
    "body",
    [
        b'{"amount": "1.00", "amount": "1000.00"}',
        b'{"amount": NaN}',
        b"\xff\xfe not utf-8",
        b"{not json",
        b"[" * 5000 + b"]" * 5000,
    ],
)
def test_malformed_json_is_a_400(client: TestClient, body: bytes) -> None:
    response = client.post(
        "/v1/transactions", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert _error_code(response) == "REQUEST_MALFORMED_JSON"


def test_a_json_array_is_not_an_event(client: TestClient) -> None:
    response = _post(client, [support.event()])
    assert response.status_code == 422
    assert _error_code(response) == "EVENT_NOT_AN_OBJECT"


def test_other_media_types_are_refused(client: TestClient) -> None:
    response = client.post(
        "/v1/transactions",
        content=json.dumps(support.event()),
        headers={"content-type": "text/plain"},
    )
    assert response.status_code == 415
    assert _error_code(response) == "REQUEST_UNSUPPORTED_MEDIA_TYPE"


def test_oversized_bodies_are_refused(client: TestClient) -> None:
    padded = support.event(event_id="x" * 10) | {"padding": "y" * MAX_BODY_BYTES}
    response = _post(client, padded)
    assert response.status_code == 413
    assert _error_code(response) == "REQUEST_TOO_LARGE"


def test_oversized_streamed_bodies_are_refused(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(40):
            yield b" " * 1024

    response = client.post(
        "/v1/transactions", content=chunks(), headers={"content-type": "application/json"}
    )
    assert response.status_code == 413


def test_a_decision_can_be_fetched(client: TestClient) -> None:
    created = _post(client, support.event()).json()
    response = client.get(f"/v1/decisions/{created['decision_id']}")
    assert response.status_code == 200
    assert response.json() == {k: v for k, v in created.items() if k != "replayed"}


def test_an_unknown_decision_is_a_404(client: TestClient) -> None:
    response = client.get(f"/v1/decisions/{uuid4()}")
    assert response.status_code == 404
    assert _error_code(response) == "DECISION_NOT_FOUND"


def test_the_audit_endpoint_verifies_the_decision(client: TestClient) -> None:
    created = _post(client, support.event(amount="600.00")).json()
    response = client.get(f"/v1/decisions/{created['decision_id']}/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["verification"]["ok"] is True
    assert body["record"]["outcome"] == created["outcome"] == "REVIEW"
    assert body["decision"]["decision_id"] == created["decision_id"]


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/v1/nowhere", 404, "ROUTE_NOT_FOUND"),
        ("GET", "/v1/decisions/not-a-uuid", 404, "ROUTE_NOT_FOUND"),
        ("GET", "/v1/transactions", 405, "METHOD_NOT_ALLOWED"),
        ("DELETE", "/healthz", 405, "METHOD_NOT_ALLOWED"),
    ],
)
def test_routing_errors_are_json(
    client: TestClient, method: str, path: str, status: int, code: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    assert _error_code(response) == code


def test_health_readiness_and_metrics(client: TestClient, service: DecisionService) -> None:
    _post(client, support.event())
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz").json()
    assert ready == {"status": "ready", "ruleset_version": service.ruleset.version}
    metrics = client.get("/metrics")
    assert metrics.headers["content-type"].startswith("text/plain")
    assert 'sentinel_decisions_total{outcome="APPROVE"} 1' in metrics.text
    assert "sentinel_decision_latency_seconds_count 1" in metrics.text


def test_an_unavailable_database_is_a_503(migrated_url: str, pool: Pool) -> None:
    closed = create_pool(migrated_url, min_size=1, max_size=1)
    service = DecisionService(closed, support.ruleset(support.CEILING))
    closed.close()
    with TestClient(create_app(service)) as client:
        assert _post(client, support.event()).status_code == 503
        assert client.get("/readyz").status_code == 503
        assert _error_code(client.get(f"/v1/decisions/{uuid4()}")) == "STORE_UNAVAILABLE"


def test_unexpected_errors_are_a_generic_500(service: DecisionService) -> None:
    def explode(raw: object) -> None:
        raise RuntimeError("secret internal detail")

    service.submit = explode  # type: ignore[assignment,method-assign]
    with TestClient(create_app(service), raise_server_exceptions=False) as client:
        response = _post(client, support.event())
    assert response.status_code == 500
    assert _error_code(response) == "INTERNAL_ERROR"
    assert "secret" not in response.text


def test_the_production_factory_builds_a_working_app(
    migrated_url: str, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTINEL_DATABASE_URL", migrated_url)
    monkeypatch.setenv("SENTINEL_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("SENTINEL_POOL_MAX_SIZE", "2")
    with TestClient(app_from_env()) as client:
        assert client.get("/readyz").status_code == 200
        assert _post(client, support.event()).status_code == 201
