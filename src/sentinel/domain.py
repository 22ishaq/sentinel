"""Transaction events: strict parsing of untrusted input into validated domain values.

The canonical form of an event (:meth:`TransactionEvent.to_canonical`) uses the same schema
as the input, so the event stored in an audit record can be parsed again to replay it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from sentinel.canonical import digest, format_timestamp
from sentinel.errors import ErrorCode, JsonValue, MoneyError, ValidationError, preview
from sentinel.money import Money, normalise, transactable_currency


class Outcome(StrEnum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    DECLINE = "DECLINE"

    @property
    def severity(self) -> int:
        return _SEVERITY[self]


_SEVERITY: Final = {Outcome.APPROVE: 0, Outcome.REVIEW: 1, Outcome.DECLINE: 2}


def most_severe(outcomes: Iterable[Outcome]) -> Outcome:
    return max(outcomes, key=lambda outcome: outcome.severity, default=Outcome.APPROVE)


class Channel(StrEnum):
    CARD_PRESENT = "card_present"
    ECOMMERCE = "ecommerce"
    ATM = "atm"
    TRANSFER = "transfer"


@dataclass(frozen=True, slots=True)
class Merchant:
    id: str
    category_code: str
    country: str


@dataclass(frozen=True, slots=True)
class TransactionEvent:
    event_id: str
    occurred_at: datetime
    account_id: str
    account_country: str
    amount: Money
    channel: Channel
    merchant: Merchant

    def to_canonical(self) -> dict[str, JsonValue]:
        return {
            "event_id": self.event_id,
            "occurred_at": format_timestamp(self.occurred_at),
            "account": {"id": self.account_id, "country": self.account_country},
            "amount": self.amount.format(),
            "currency": self.amount.currency.code,
            "channel": self.channel.value,
            "merchant": {
                "id": self.merchant.id,
                "category_code": self.merchant.category_code,
                "country": self.merchant.country,
            },
        }

    def fingerprint(self) -> str:
        """Digest of the normalised event: ``"12.5"`` and ``"12.50"`` GBP fingerprint the same."""
        return digest(self.to_canonical())


_IDENTIFIER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_COUNTRY: Final = re.compile(r"[A-Z]{2}")
_CATEGORY_CODE: Final = re.compile(r"[0-9]{4}")
_RFC3339: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)


class _Fields:
    """Reads one JSON object, tracking its path for error details and rejecting extras."""

    def __init__(self, data: object, path: str, allowed: frozenset[str]) -> None:
        if not isinstance(data, dict):
            if not path:
                raise ValidationError(
                    ErrorCode.EVENT_NOT_AN_OBJECT,
                    "a transaction event must be a JSON object",
                    details={"type": type(data).__name__},
                )
            raise ValidationError(
                ErrorCode.EVENT_FIELD_INVALID,
                f"{path} must be an object",
                details={"field": path, "reason": "not_an_object"},
            )
        unknown = sorted(self._join(path, str(key)) for key in data if key not in allowed)
        if unknown:
            raise ValidationError(
                ErrorCode.EVENT_FIELD_UNKNOWN,
                f"unexpected field(s): {', '.join(unknown)}",
                details={"fields": list(unknown)},
            )
        self._data: dict[str, object] = data
        self._path = path

    @staticmethod
    def _join(path: str, name: str) -> str:
        return f"{path}.{name}" if path else name

    def path(self, name: str) -> str:
        return self._join(self._path, name)

    def raw(self, name: str) -> object:
        if name not in self._data:
            raise ValidationError(
                ErrorCode.EVENT_FIELD_MISSING,
                f"{self.path(name)} is required",
                details={"field": self.path(name)},
            )
        return self._data[name]

    def text(self, name: str, pattern: re.Pattern[str], expected: str) -> str:
        value = self.raw(name)
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValidationError(
                ErrorCode.EVENT_FIELD_INVALID,
                f"{self.path(name)} must be {expected}",
                details={"field": self.path(name), "value": preview(value)},
            )
        return value

    def nested(self, name: str, allowed: frozenset[str]) -> _Fields:
        return _Fields(self.raw(name), self.path(name), allowed)


_EVENT_FIELDS: Final = frozenset(
    {"event_id", "occurred_at", "account", "amount", "currency", "channel", "merchant"}
)
_ACCOUNT_FIELDS: Final = frozenset({"id", "country"})
_MERCHANT_FIELDS: Final = frozenset({"id", "category_code", "country"})


def _timestamp(fields: _Fields, name: str) -> datetime:
    text = fields.text(name, _RFC3339, "an RFC 3339 timestamp with a UTC offset")
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as error:  # e.g. month 13: matches the shape, not the calendar
        raise ValidationError(
            ErrorCode.EVENT_FIELD_INVALID,
            f"{fields.path(name)} is not a valid timestamp",
            details={"field": fields.path(name), "value": preview(text)},
        ) from error


def parse_transaction(
    raw: object,
    *,
    now: datetime | None = None,
    max_future_skew: timedelta = timedelta(minutes=5),
) -> TransactionEvent:
    """Validate an untrusted event. Raises ``ValidationError`` or ``MoneyError`` with a code."""
    fields = _Fields(raw, "", _EVENT_FIELDS)
    event_id = fields.text("event_id", _IDENTIFIER, "1-128 characters of [A-Za-z0-9._:-]")
    occurred_at = _timestamp(fields, "occurred_at")
    if now is not None and occurred_at > now + max_future_skew:
        raise ValidationError(
            ErrorCode.EVENT_TIMESTAMP_IN_FUTURE,
            "occurred_at is further in the future than the allowed clock skew",
            details={
                "field": "occurred_at",
                "max_skew_seconds": int(max_future_skew.total_seconds()),
            },
        )

    account = fields.nested("account", _ACCOUNT_FIELDS)
    account_id = account.text("id", _IDENTIFIER, "1-128 characters of [A-Za-z0-9._:-]")
    account_country = account.text("country", _COUNTRY, "an ISO 3166-1 alpha-2 code")

    try:
        cur = transactable_currency(fields.raw("currency"))
    except MoneyError as error:
        raise error.with_details(field="currency") from error
    try:
        amount = normalise(fields.raw("amount"), cur)
    except MoneyError as error:
        raise error.with_details(field="amount") from error
    if not amount.is_positive():
        raise ValidationError(
            ErrorCode.EVENT_AMOUNT_NOT_POSITIVE,
            "amount must be greater than zero",
            details={"field": "amount", "amount": amount.format()},
        )

    channel_raw = fields.raw("channel")
    try:
        channel = Channel(channel_raw) if isinstance(channel_raw, str) else None
    except ValueError:
        channel = None
    if channel is None:
        raise ValidationError(
            ErrorCode.EVENT_FIELD_INVALID,
            f"channel must be one of: {', '.join(c.value for c in Channel)}",
            details={"field": "channel", "value": preview(channel_raw)},
        )

    merchant = fields.nested("merchant", _MERCHANT_FIELDS)
    return TransactionEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        account_id=account_id,
        account_country=account_country,
        amount=amount,
        channel=channel,
        merchant=Merchant(
            id=merchant.text("id", _IDENTIFIER, "1-128 characters of [A-Za-z0-9._:-]"),
            category_code=merchant.text("category_code", _CATEGORY_CODE, "a 4-digit ISO 18245 MCC"),
            country=merchant.text("country", _COUNTRY, "an ISO 3166-1 alpha-2 code"),
        ),
    )


class _DuplicateKeyError(ValueError):
    pass


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constants(name: str) -> object:
    raise ValueError(f"{name} is not valid JSON")


MAX_JSON_DEPTH: Final = 32


def _depth(value: object) -> int:
    deepest, stack = 0, [(value, 1)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict | list):
            deepest = max(deepest, level)
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, level + 1) for child in children)
    return deepest


def _malformed(reason: str) -> ValidationError:
    return ValidationError(
        ErrorCode.REQUEST_MALFORMED_JSON,
        "request body is not valid JSON",
        details={"reason": preview(reason, 160)},
    )


def parse_json(body: bytes) -> object:
    """Strict JSON: UTF-8 only, no NaN/Infinity, no duplicate keys, bounded nesting.

    Duplicate keys are rejected because parsers disagree on which value wins; a payload
    that one component reads as ``"amount": "1"`` must not be read as ``"1000"`` by another.
    """
    try:
        value = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_keys, parse_constant=_no_constants
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise _malformed(str(error)) from error
    if _depth(value) > MAX_JSON_DEPTH:
        raise _malformed(f"nesting deeper than {MAX_JSON_DEPTH} levels")
    return value
