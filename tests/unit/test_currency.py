from __future__ import annotations

import re

import pytest

from sentinel.errors import ErrorCode, MoneyError
from sentinel.money import CURRENCIES, currency, normalise


def test_table_entries_are_well_formed() -> None:
    for code, entry in CURRENCIES.items():
        assert code == entry.code
        assert re.fullmatch(r"[A-Z]{3}", code)
        assert re.fullmatch(r"[0-9]{3}", entry.numeric)
        assert entry.minor_unit in {None, 0, 2, 3, 4}
        assert entry.name


def test_numeric_codes_are_unique() -> None:
    numerics = [entry.numeric for entry in CURRENCIES.values()]
    assert len(numerics) == len(set(numerics))


@pytest.mark.parametrize(
    ("code", "minor_unit"),
    [
        ("GBP", 2),
        ("USD", 2),
        ("EUR", 2),
        ("JPY", 0),
        ("KRW", 0),
        ("ISK", 0),
        ("KWD", 3),
        ("BHD", 3),
        ("TND", 3),
        ("CLF", 4),
        ("UYW", 4),
    ],
)
def test_minor_units_follow_iso_4217(code: str, minor_unit: int) -> None:
    assert currency(code).minor_unit == minor_unit


@pytest.mark.parametrize("code", ["ANG", "BGN", "CUC", "HRK", "SLL", "VEF", "ZWL"])
def test_withdrawn_codes_are_unknown(code: str) -> None:
    with pytest.raises(MoneyError) as caught:
        currency(code)
    assert caught.value.code is ErrorCode.CURRENCY_UNKNOWN


@pytest.mark.parametrize("code", ["gbp", "Gbp", "GB", "GBPX", " GBP", "", "826", None, 826])
def test_malformed_codes_are_rejected(code: object) -> None:
    with pytest.raises(MoneyError) as caught:
        currency(code)
    assert caught.value.code is ErrorCode.CURRENCY_MALFORMED


@pytest.mark.parametrize("code", ["XAU", "XAG", "XDR", "XTS", "XXX"])
def test_codes_without_a_minor_unit_cannot_carry_amounts(code: str) -> None:
    assert not currency(code).transactable
    with pytest.raises(MoneyError) as caught:
        normalise("1", code)
    assert caught.value.code is ErrorCode.CURRENCY_NOT_TRANSACTABLE
