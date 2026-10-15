from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sentinel.canonical import canonical_json, digest, format_timestamp
from sentinel.domain import Channel, Outcome, most_severe, parse_json, parse_transaction
from sentinel.errors import ErrorCode, SentinelError, ValidationError
from tests import support


def _code(raw: object) -> ErrorCode:
    with pytest.raises(SentinelError) as caught:
        parse_transaction(raw)
    return caught.value.code


def test_a_valid_event_parses_into_domain_values() -> None:
    event = parse_transaction(
        support.event(occurred_at="2026-10-15T10:30:00+01:00", amount="12.5", channel="ecommerce")
    )
    assert event.occurred_at == datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
    assert event.amount.minor_units == 1250
    assert event.channel is Channel.ECOMMERCE
    assert event.merchant.category_code == "5411"


def test_the_canonical_form_parses_back_to_the_same_event() -> None:
    event = parse_transaction(support.event())
    assert parse_transaction(event.to_canonical()) == event


def test_fingerprints_ignore_representation_but_not_content() -> None:
    base = support.event(amount="12.50")
    same = parse_transaction({**base, "amount": "12.5"}).fingerprint()
    assert parse_transaction(base).fingerprint() == same
    assert parse_transaction({**base, "amount": "12.51"}).fingerprint() != same


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda e: e.pop("event_id"), ErrorCode.EVENT_FIELD_MISSING),
        (lambda e: e["account"].pop("country"), ErrorCode.EVENT_FIELD_MISSING),
        (lambda e: e.update(extra=1), ErrorCode.EVENT_FIELD_UNKNOWN),
        (lambda e: e["merchant"].update(name="Shop"), ErrorCode.EVENT_FIELD_UNKNOWN),
        (lambda e: e.update(event_id="has space"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(event_id="x" * 129), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(event_id=123), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(occurred_at="2026-10-15T09:30:00"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(occurred_at="2026-13-45T09:30:00Z"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(occurred_at="15/10/2026"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(account="acc_1"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e["account"].update(country="gb"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e["merchant"].update(category_code="54111"), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(channel=None), ErrorCode.EVENT_FIELD_INVALID),
        (lambda e: e.update(amount="-1.00"), ErrorCode.EVENT_AMOUNT_NOT_POSITIVE),
        (lambda e: e.update(amount=1.5), ErrorCode.MONEY_FLOAT_REJECTED),
        (lambda e: e.update(currency="XXX"), ErrorCode.CURRENCY_NOT_TRANSACTABLE),
    ],
)
def test_invalid_events_fail_with_a_code(mutate: Any, code: ErrorCode) -> None:
    payload = support.event()
    mutate(payload)
    assert _code(payload) is code


@pytest.mark.parametrize("raw", [[], "event", 42, None])
def test_non_objects_are_rejected(raw: object) -> None:
    assert _code(raw) is ErrorCode.EVENT_NOT_AN_OBJECT


def test_errors_name_nested_fields() -> None:
    payload = support.event()
    payload["merchant"]["country"] = "GBR"
    with pytest.raises(ValidationError) as caught:
        parse_transaction(payload)
    assert caught.value.details["field"] == "merchant.country"


def test_future_timestamps_are_rejected_beyond_the_skew() -> None:
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    near = support.event(occurred_at=format_timestamp(now + timedelta(minutes=4)))
    far = support.event(occurred_at=format_timestamp(now + timedelta(minutes=6)))
    parse_transaction(near, now=now)
    with pytest.raises(ValidationError) as caught:
        parse_transaction(far, now=now)
    assert caught.value.code is ErrorCode.EVENT_TIMESTAMP_IN_FUTURE


def test_outcomes_are_ordered_by_severity() -> None:
    assert most_severe([Outcome.REVIEW, Outcome.DECLINE, Outcome.APPROVE]) is Outcome.DECLINE
    assert most_severe([]) is Outcome.APPROVE


@pytest.mark.parametrize(
    "body",
    [
        b'{"a": 1, "a": 2}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b"\xc3\x28",
        b"",
        b"[" * 40 + b"]" * 40,
        b'{"a": ' + b"9" * 5000 + b"}",
    ],
)
def test_parse_json_is_strict(body: bytes) -> None:
    with pytest.raises(ValidationError) as caught:
        parse_json(body)
    assert caught.value.code is ErrorCode.REQUEST_MALFORMED_JSON


def test_parse_json_keeps_floats_for_the_money_layer_to_reject() -> None:
    assert parse_json(b'{"amount": 12.5}') == {"amount": 12.5}


def test_canonical_json_is_deterministic() -> None:
    assert canonical_json({"b": [1, {"d": True, "c": None}], "a": "é"}) == (
        '{"a":"é","b":[1,{"c":null,"d":true}]}'.encode()
    )
    assert digest({"x": 1, "y": 2}) == digest({"y": 2, "x": 1})


@pytest.mark.parametrize(
    "value", [1.5, {"a": [0.1]}, {1: "int key"}, {"a": object()}, {"a": {1, 2}}]
)
def test_canonical_json_refuses_non_canonical_values(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_json(value)


def test_timestamps_are_formatted_in_utc_and_naive_ones_refused() -> None:
    moment = datetime(2026, 10, 15, 10, 30, 0, 12, tzinfo=UTC) + timedelta(hours=0)
    assert format_timestamp(moment) == "2026-10-15T10:30:00.000012Z"
    with pytest.raises(ValueError, match="naive"):
        format_timestamp(datetime(2026, 10, 15))  # noqa: DTZ001 - the point of the test
