"""Property-based tests for money normalisation.

These run the normaliser against values far outside what hand-written examples cover:
every transactable ISO 4217 currency and amounts up to the full 38-digit storage width.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sentinel.errors import ErrorCode, MoneyError
from sentinel.money import CURRENCIES, MAX_MINOR_UNITS, Currency, Money, normalise

TRANSACTABLE = sorted((c for c in CURRENCIES.values() if c.transactable), key=lambda c: c.code)

currencies = st.sampled_from(TRANSACTABLE)
minor_units = st.integers(min_value=-MAX_MINOR_UNITS, max_value=MAX_MINOR_UNITS)
monies = st.builds(Money, minor_units, currencies)


def _same_currency_pair(cur: Currency) -> st.SearchStrategy[tuple[Money, Money]]:
    return st.tuples(st.builds(Money, minor_units, st.just(cur)), st.builds(Money, minor_units, st.just(cur)))


@given(monies)
def test_formatted_amount_round_trips(money: Money) -> None:
    assert normalise(money.format(), money.currency) == money


@given(monies)
def test_decimal_amount_round_trips(money: Money) -> None:
    assert normalise(money.to_decimal(), money.currency) == money


@given(monies, st.integers(min_value=1, max_value=20))
def test_trailing_zeros_do_not_count_as_precision(money: Money, zeros: int) -> None:
    padded = money.format() + ("0" * zeros if money.exponent else "." + "0" * zeros)
    assert normalise(padded, money.currency) == money


@given(monies, st.integers(min_value=0, max_value=10), st.integers(min_value=1, max_value=9))
def test_digits_beyond_the_minor_unit_are_rejected_never_rounded(
    money: Money, zeros: int, digit: int
) -> None:
    text = money.format() + ("" if money.exponent else ".") + "0" * zeros + str(digit)
    with pytest.raises(MoneyError) as caught:
        normalise(text, money.currency)
    assert caught.value.code is ErrorCode.MONEY_PRECISION_EXCEEDED


@given(st.decimals(allow_nan=False, allow_infinity=False), currencies)
def test_accepted_decimals_are_represented_exactly(value: Decimal, cur: Currency) -> None:
    try:
        money = normalise(value, cur)
    except MoneyError as error:
        assert error.code in {ErrorCode.MONEY_PRECISION_EXCEEDED, ErrorCode.MONEY_OUT_OF_RANGE}
    else:
        assert money.to_decimal() == value


@given(st.floats(allow_nan=True, allow_infinity=True), currencies)
def test_floats_are_always_rejected(value: float, cur: Currency) -> None:
    with pytest.raises(MoneyError) as caught:
        normalise(value, cur)
    assert caught.value.code is ErrorCode.MONEY_FLOAT_REJECTED


@given(st.text(max_size=120))
def test_arbitrary_text_is_accepted_or_rejected_with_a_typed_error(text: str) -> None:
    try:
        money = normalise(text, "GBP")
    except MoneyError as error:
        assert error.code in {
            ErrorCode.MONEY_MALFORMED,
            ErrorCode.MONEY_PRECISION_EXCEEDED,
            ErrorCode.MONEY_OUT_OF_RANGE,
        }
    else:
        assert money.to_decimal() == Decimal(text)


@given(currencies.flatmap(_same_currency_pair))
def test_addition_is_exact_or_rejected(pair: tuple[Money, Money]) -> None:
    left, right = pair
    exact = left.minor_units + right.minor_units
    if abs(exact) > MAX_MINOR_UNITS:
        with pytest.raises(MoneyError) as caught:
            _ = left + right
        assert caught.value.code is ErrorCode.MONEY_OUT_OF_RANGE
    else:
        total = left + right
        assert total.minor_units == exact
        assert total - right == left
