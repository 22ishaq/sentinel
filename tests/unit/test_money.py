from __future__ import annotations

import time
from decimal import Decimal

import pytest

from sentinel.errors import ErrorCode, MoneyError
from sentinel.money import MAX_MINOR_UNITS, Money, currency, normalise

GBP = currency("GBP")
JPY = currency("JPY")
KWD = currency("KWD")


def _code(amount: object, code: str = "GBP") -> ErrorCode:
    with pytest.raises(MoneyError) as caught:
        normalise(amount, code)
    return caught.value.code


@pytest.mark.parametrize(
    ("amount", "code", "minor"),
    [
        ("0", "GBP", 0),
        ("12.34", "GBP", 1234),
        ("12.3", "GBP", 1230),
        ("12", "GBP", 1200),
        ("12.340", "GBP", 1234),
        ("-5.01", "EUR", -501),
        ("+7.5", "USD", 750),
        ("007.50", "GBP", 750),
        ("1500", "JPY", 1500),
        ("1500.000", "JPY", 1500),
        ("1.234", "KWD", 1234),
        ("0.0001", "CLF", 1),
        (12, "GBP", 1200),
        (Decimal("12.34"), "GBP", 1234),
        (Decimal("1.2E+3"), "GBP", 120000),
        (Decimal("-0.00"), "GBP", 0),
    ],
)
def test_normalise_is_exact(amount: object, code: str, minor: int) -> None:
    money = normalise(amount, code)
    assert money.minor_units == minor
    assert money.currency.code == code


def test_amounts_wider_than_the_default_decimal_context_are_exact() -> None:
    # Regression: the default decimal context keeps 28 significant digits, and the first
    # Decimal-based conversion returned 1234567890123456789012345679000 for this amount.
    money = normalise("12345678901234567890123456789.12", "USD")
    assert money.minor_units == 1234567890123456789012345678912
    assert money.format() == "12345678901234567890123456789.12"


def test_extra_digits_on_a_wide_amount_are_rejected_not_rounded() -> None:
    # Regression: this was rounded to 13106511809631223643 and accepted.
    assert _code("131065118096312236.43000000001", "AED") is ErrorCode.MONEY_PRECISION_EXCEEDED


@pytest.mark.parametrize(
    ("amount", "code"),
    [
        ("12.345", "GBP"),
        ("0.001", "USD"),
        ("1500.5", "JPY"),
        ("1.2345", "KWD"),
        ("0.00001", "CLF"),
        (Decimal("0.005"), "EUR"),
        (Decimal("1E-3"), "GBP"),
    ],
)
def test_digits_beyond_the_minor_unit_are_rejected(amount: object, code: str) -> None:
    assert _code(amount, code) is ErrorCode.MONEY_PRECISION_EXCEEDED


@pytest.mark.parametrize("amount", [0.1, 12.0, -0.0, float("nan"), float("inf")])
def test_floats_are_rejected(amount: float) -> None:
    assert _code(amount) is ErrorCode.MONEY_FLOAT_REJECTED


@pytest.mark.parametrize(
    "amount",
    [
        "",
        " 1.00",
        "1.00 ",
        "1,000.00",
        "1_000",
        "1e3",
        ".5",
        "5.",
        "0x10",
        "NaN",
        "Infinity",
        "--1",
        "12.3.4",
        "£12",
        "".join(map(chr, (0x661, 0x662, 0x663))),  # Arabic-Indic digits: Decimal() accepts them
        "".join(map(chr, (0xFF11, 0xFF12))),  # full-width digits
        "1" * 101,
    ],
)
def test_malformed_strings_are_rejected(amount: str) -> None:
    assert _code(amount) is ErrorCode.MONEY_MALFORMED


@pytest.mark.parametrize("amount", [True, False, None, [1], {"value": 1}, b"12"])
def test_other_types_are_rejected(amount: object) -> None:
    assert _code(amount) is ErrorCode.MONEY_INVALID_TYPE


@pytest.mark.parametrize("amount", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_non_finite_decimals_are_rejected(amount: str) -> None:
    assert _code(Decimal(amount)) is ErrorCode.MONEY_NOT_FINITE


def test_range_is_the_storage_width() -> None:
    assert normalise(str(MAX_MINOR_UNITS), "JPY").minor_units == MAX_MINOR_UNITS
    assert _code(str(MAX_MINOR_UNITS + 1), "JPY") is ErrorCode.MONEY_OUT_OF_RANGE
    assert _code("1" + "0" * 36, "GBP") is ErrorCode.MONEY_OUT_OF_RANGE


def test_hostile_exponents_are_rejected_cheaply() -> None:
    started = time.perf_counter()
    assert _code(Decimal("1E+999999")) is ErrorCode.MONEY_OUT_OF_RANGE
    assert _code(Decimal("1E-999999")) is ErrorCode.MONEY_PRECISION_EXCEEDED
    assert normalise(Decimal("0E+999999"), "GBP").minor_units == 0
    assert time.perf_counter() - started < 0.5


def test_error_details_echo_a_bounded_preview_of_the_input() -> None:
    with pytest.raises(MoneyError) as caught:
        normalise("x" * 500, "GBP")
    echoed = caught.value.details["amount"]
    assert isinstance(echoed, str)
    assert len(echoed) <= 64


@pytest.mark.parametrize(
    ("money", "text"),
    [
        (Money(123450, GBP), "1234.50"),
        (Money(-5, GBP), "-0.05"),
        (Money(1500, JPY), "1500"),
        (Money(1, KWD), "0.001"),
        (Money(0, GBP), "0.00"),
    ],
)
def test_format_uses_the_full_minor_unit_width(money: Money, text: str) -> None:
    assert money.format() == text
    assert str(money) == f"{text} {money.currency.code}"
    assert money.to_json() == {"amount": text, "currency": money.currency.code}
    assert money.to_decimal() == Decimal(text)


def test_same_currency_arithmetic_and_comparison() -> None:
    a, b = Money(1000, GBP), Money(250, GBP)
    assert (a + b).minor_units == 1250
    assert (a - b).minor_units == 750
    assert (-a).minor_units == -1000
    assert b < a
    assert b <= a
    assert a > b
    assert a >= b
    assert a.is_positive()
    assert not Money.zero("GBP").is_positive()


@pytest.mark.parametrize(
    "operation",
    [
        lambda a, b: a + b,
        lambda a, b: a - b,
        lambda a, b: a < b,
        lambda a, b: a <= b,
        lambda a, b: a > b,
        lambda a, b: a >= b,
    ],
)
def test_mixing_currencies_is_an_error(operation: object) -> None:
    assert callable(operation)
    with pytest.raises(MoneyError) as caught:
        operation(Money(1, GBP), Money(1, JPY))
    assert caught.value.code is ErrorCode.MONEY_CURRENCY_MISMATCH


def test_equality_across_currencies_is_simply_false() -> None:
    assert Money(100, GBP) != Money(100, JPY)
    assert Money(100, GBP) == Money(100, currency("GBP"))


def test_arithmetic_with_non_money_is_unsupported() -> None:
    with pytest.raises(TypeError):
        _ = Money(1, GBP) + 1
    with pytest.raises(TypeError):
        _ = Money(1, GBP) - 1
    with pytest.raises(TypeError):
        _ = Money(1, GBP) < 1


def test_overflowing_arithmetic_is_rejected() -> None:
    with pytest.raises(MoneyError) as caught:
        _ = Money(MAX_MINOR_UNITS, JPY) + Money(1, JPY)
    assert caught.value.code is ErrorCode.MONEY_OUT_OF_RANGE


def test_constructor_validates_its_inputs() -> None:
    with pytest.raises(MoneyError) as caught:
        Money(True, GBP)
    assert caught.value.code is ErrorCode.MONEY_INVALID_TYPE
    with pytest.raises(MoneyError) as caught:
        Money(1, currency("XAU"))
    assert caught.value.code is ErrorCode.CURRENCY_NOT_TRANSACTABLE
