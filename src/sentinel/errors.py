"""Typed errors with stable, machine-readable codes.

Everything that crosses a trust boundary (HTTP, CLI, configuration files) fails with a
:class:`SentinelError`. Clients branch on ``code``, which is part of the public contract;
``message`` is for humans and may be reworded between releases; ``details`` carries the
structured context (field name, limits, offending value) needed to act on the error.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None


class ErrorCode(StrEnum):
    # Money and currency
    MONEY_FLOAT_REJECTED = "MONEY_FLOAT_REJECTED"
    MONEY_INVALID_TYPE = "MONEY_INVALID_TYPE"
    MONEY_MALFORMED = "MONEY_MALFORMED"
    MONEY_NOT_FINITE = "MONEY_NOT_FINITE"
    MONEY_PRECISION_EXCEEDED = "MONEY_PRECISION_EXCEEDED"
    MONEY_OUT_OF_RANGE = "MONEY_OUT_OF_RANGE"
    MONEY_CURRENCY_MISMATCH = "MONEY_CURRENCY_MISMATCH"
    CURRENCY_MALFORMED = "CURRENCY_MALFORMED"
    CURRENCY_UNKNOWN = "CURRENCY_UNKNOWN"
    CURRENCY_NOT_TRANSACTABLE = "CURRENCY_NOT_TRANSACTABLE"

    # Request envelope
    REQUEST_MALFORMED_JSON = "REQUEST_MALFORMED_JSON"
    REQUEST_TOO_LARGE = "REQUEST_TOO_LARGE"
    REQUEST_UNSUPPORTED_MEDIA_TYPE = "REQUEST_UNSUPPORTED_MEDIA_TYPE"

    # Transaction event validation
    EVENT_NOT_AN_OBJECT = "EVENT_NOT_AN_OBJECT"
    EVENT_FIELD_MISSING = "EVENT_FIELD_MISSING"
    EVENT_FIELD_UNKNOWN = "EVENT_FIELD_UNKNOWN"
    EVENT_FIELD_INVALID = "EVENT_FIELD_INVALID"
    EVENT_AMOUNT_NOT_POSITIVE = "EVENT_AMOUNT_NOT_POSITIVE"
    EVENT_TIMESTAMP_IN_FUTURE = "EVENT_TIMESTAMP_IN_FUTURE"

    # Idempotency and lookup
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    DECISION_NOT_FOUND = "DECISION_NOT_FOUND"

    # Configuration and operations
    RULESET_INVALID = "RULESET_INVALID"
    MIGRATION_CHECKSUM_MISMATCH = "MIGRATION_CHECKSUM_MISMATCH"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


HTTP_STATUS: Final[Mapping[ErrorCode, int]] = {
    ErrorCode.MONEY_FLOAT_REJECTED: 422,
    ErrorCode.MONEY_INVALID_TYPE: 422,
    ErrorCode.MONEY_MALFORMED: 422,
    ErrorCode.MONEY_NOT_FINITE: 422,
    ErrorCode.MONEY_PRECISION_EXCEEDED: 422,
    ErrorCode.MONEY_OUT_OF_RANGE: 422,
    ErrorCode.MONEY_CURRENCY_MISMATCH: 422,
    ErrorCode.CURRENCY_MALFORMED: 422,
    ErrorCode.CURRENCY_UNKNOWN: 422,
    ErrorCode.CURRENCY_NOT_TRANSACTABLE: 422,
    ErrorCode.REQUEST_MALFORMED_JSON: 400,
    ErrorCode.REQUEST_TOO_LARGE: 413,
    ErrorCode.REQUEST_UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.EVENT_NOT_AN_OBJECT: 422,
    ErrorCode.EVENT_FIELD_MISSING: 422,
    ErrorCode.EVENT_FIELD_UNKNOWN: 422,
    ErrorCode.EVENT_FIELD_INVALID: 422,
    ErrorCode.EVENT_AMOUNT_NOT_POSITIVE: 422,
    ErrorCode.EVENT_TIMESTAMP_IN_FUTURE: 422,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.DECISION_NOT_FOUND: 404,
    ErrorCode.RULESET_INVALID: 500,
    ErrorCode.MIGRATION_CHECKSUM_MISMATCH: 500,
    ErrorCode.STORE_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class SentinelError(Exception):
    """Base class: a stable ``code``, a human ``message`` and structured ``details``."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        details: Mapping[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details: dict[str, JsonValue] = dict(details or {})

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    def with_details(self, **extra: JsonValue) -> SentinelError:
        """Return a copy of this error (same class and code) with extra context merged in."""
        clone = type(self)(self.code, self.message, details={**self.details, **extra})
        clone.__cause__ = self
        return clone

    def to_dict(self) -> dict[str, JsonValue]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


class MoneyError(SentinelError):
    """An amount or currency was rejected. Raised instead of rounding or guessing."""


class ValidationError(SentinelError):
    """A transaction event or request envelope failed validation."""


class ConflictError(SentinelError):
    """The request conflicts with state already recorded (e.g. an idempotency key reused)."""


class NotFoundError(SentinelError):
    """The requested resource does not exist."""


class ConfigurationError(SentinelError):
    """Static configuration (ruleset, migrations) is invalid. Fail at startup, not per request."""


class StoreUnavailableError(SentinelError):
    """PostgreSQL could not be reached in time."""


def preview(value: object, limit: int = 64) -> str:
    """A bounded ``repr`` for echoing untrusted input back inside error details."""
    text = repr(value)
    return text if len(text) <= limit else f"{text[: limit - 3]}..."
