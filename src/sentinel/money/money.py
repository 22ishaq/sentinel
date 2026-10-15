"""Exact money: an integer count of minor units tagged with an ISO 4217 currency.

:func:`normalise` is the only way an untrusted amount becomes a :class:`Money`. It never
rounds and never guesses: anything that cannot be represented exactly in the currency's
minor unit is rejected with a :class:`~sentinel.errors.MoneyError` carrying a stable code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from sentinel.errors import ErrorCode, MoneyError, preview
from sentinel.money.iso4217 import CURRENCIES, Currency

MAX_DIGITS: Final = 38
"""Widest amount, in minor units, that the store accepts: PostgreSQL ``NUMERIC(38, 0)``."""

MAX_MINOR_UNITS: Final = 10**MAX_DIGITS - 1

_AMOUNT_TEXT: Final = re.compile(r"(?P<sign>[+-]?)(?P<whole>[0-9]+)(?:\.(?P<frac>[0-9]+))?")
_CURRENCY_CODE: Final = re.compile(r"[A-Z]{3}")
_MAX_AMOUNT_TEXT_LENGTH: Final = 100


def currency(code: object) -> Currency:
    """Resolve an ISO 4217 alphabetic code. Case-sensitive: ``"gbp"`` is malformed, not GBP."""
    if not isinstance(code, str) or _CURRENCY_CODE.fullmatch(code) is None:
        raise MoneyError(
            ErrorCode.CURRENCY_MALFORMED,
            "currency must be a three-letter upper-case ISO 4217 code",
            details={"currency": preview(code)},
        )
    found = CURRENCIES.get(code)
    if found is None:
        raise MoneyError(
            ErrorCode.CURRENCY_UNKNOWN,
            f"{code} is not an active ISO 4217 currency",
            details={"currency": code},
        )
    return found


def _exponent(cur: Currency) -> int:
    if cur.minor_unit is None:
        raise MoneyError(
            ErrorCode.CURRENCY_NOT_TRANSACTABLE,
            f"{cur.code} ({cur.name}) has no minor unit and cannot carry a transaction amount",
            details={"currency": cur.code},
        )
    return cur.minor_unit


def _out_of_range(cur: Currency) -> MoneyError:
    return MoneyError(
        ErrorCode.MONEY_OUT_OF_RANGE,
        f"amount exceeds {MAX_DIGITS} digits of {cur.code} minor units",
        details={"currency": cur.code, "max_digits": MAX_DIGITS},
    )


@dataclass(frozen=True, slots=True)
class Money:
    minor_units: int
    currency: Currency

    def __post_init__(self) -> None:
        if type(self.minor_units) is not int:  # bool is an int subclass; reject it too
            raise MoneyError(
                ErrorCode.MONEY_INVALID_TYPE,
                "minor_units must be an int",
                details={"type": type(self.minor_units).__name__},
            )
        _exponent(self.currency)
        if abs(self.minor_units) > MAX_MINOR_UNITS:
            raise _out_of_range(self.currency)

    @classmethod
    def zero(cls, cur: Currency | str) -> Money:
        return cls(0, cur if isinstance(cur, Currency) else currency(cur))

    @property
    def exponent(self) -> int:
        return _exponent(self.currency)

    def to_decimal(self) -> Decimal:
        """The exact major-unit value. Built from digits, so no decimal context is involved."""
        digits = tuple(int(d) for d in str(abs(self.minor_units)))
        return Decimal((int(self.minor_units < 0), digits, -self.exponent))

    def format(self) -> str:
        """Exact major-unit string with the currency's full minor-unit width, e.g. ``1234.50``."""
        exponent = self.exponent
        sign = "-" if self.minor_units < 0 else ""
        whole, frac = divmod(abs(self.minor_units), 10**exponent)
        return f"{sign}{whole}.{frac:0{exponent}d}" if exponent else f"{sign}{whole}"

    def to_json(self) -> dict[str, str]:
        return {"amount": self.format(), "currency": self.currency.code}

    def __str__(self) -> str:
        return f"{self.format()} {self.currency.code}"

    def is_positive(self) -> bool:
        return self.minor_units > 0

    def _check_same_currency(self, other: Money, operation: str) -> None:
        if self.currency != other.currency:
            raise MoneyError(
                ErrorCode.MONEY_CURRENCY_MISMATCH,
                f"cannot {operation} {self.currency.code} and {other.currency.code}",
                details={"left": self.currency.code, "right": other.currency.code},
            )

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other, "add")
        return Money(self.minor_units + other.minor_units, self.currency)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other, "subtract")
        return Money(self.minor_units - other.minor_units, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor_units, self.currency)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other, "compare")
        return self.minor_units < other.minor_units

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other, "compare")
        return self.minor_units <= other.minor_units

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other, "compare")
        return self.minor_units > other.minor_units

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._check_same_currency(other, "compare")
        return self.minor_units >= other.minor_units


def _parse_exact(amount: object) -> Decimal:
    """Turn an untrusted amount into an exact Decimal without performing any arithmetic."""
    if isinstance(amount, bool):
        raise MoneyError(
            ErrorCode.MONEY_INVALID_TYPE, "amount must not be a boolean", details={"type": "bool"}
        )
    if isinstance(amount, float):
        raise MoneyError(
            ErrorCode.MONEY_FLOAT_REJECTED,
            "binary floats cannot represent most decimal amounts; send a decimal string",
            details={"amount": preview(amount)},
        )
    if isinstance(amount, int):
        return Decimal(amount)
    if isinstance(amount, Decimal):
        if not amount.is_finite():
            raise MoneyError(
                ErrorCode.MONEY_NOT_FINITE,
                "amount must be a finite number",
                details={"amount": preview(amount)},
            )
        return amount
    if isinstance(amount, str):
        if len(amount) > _MAX_AMOUNT_TEXT_LENGTH or _AMOUNT_TEXT.fullmatch(amount) is None:
            raise MoneyError(
                ErrorCode.MONEY_MALFORMED,
                "amount must be a plain decimal string such as '1234.50'",
                details={"amount": preview(amount)},
            )
        return Decimal(amount)
    raise MoneyError(
        ErrorCode.MONEY_INVALID_TYPE,
        "amount must be a decimal string, an int or a Decimal",
        details={"type": type(amount).__name__},
    )


def _to_minor_units(value: Decimal, cur: Currency) -> int:
    scaled = value * (10 ** _exponent(cur))
    if scaled != scaled.to_integral_value():
        raise MoneyError(
            ErrorCode.MONEY_PRECISION_EXCEEDED,
            f"{cur.code} has {cur.minor_unit} decimal places; refusing to round",
            details={"currency": cur.code, "minor_unit": cur.minor_unit},
        )
    minor = int(scaled)
    if abs(minor) > MAX_MINOR_UNITS:
        raise _out_of_range(cur)
    return minor


def normalise(amount: object, currency_code: str | Currency) -> Money:
    """Convert an untrusted major-unit amount into exact minor units, or raise ``MoneyError``.

    Accepted: plain decimal strings (``"12.50"``, ``"-3"``, ``"0.010"``), ints (whole major
    units) and finite ``Decimal`` values. Rejected with a typed code: floats, booleans,
    NaN/Infinity, exponent or locale notation in strings, non-ASCII digits, unknown or
    non-transactable currencies, and any amount that would need rounding to fit the
    currency's minor unit.
    """
    cur = currency_code if isinstance(currency_code, Currency) else currency(currency_code)
    _exponent(cur)
    return Money(_to_minor_units(_parse_exact(amount), cur), cur)
