# ADR 0001: Money as exact integers, and the 28-digit bug

**Status:** accepted

## Context

A decisioning system compares amounts against limits, sums them over windows, and fingerprints them
for idempotency. Any rounding along the way changes decisions. It does so silently, and differently
on each side of an integration. Amounts arrive from untrusted clients as JSON, which has no decimal
type of its own.

## Decision

- **Representation.** A `Money` is an `int` count of minor units plus an ISO 4217 currency. The
  minor unit comes from ISO's List One: 0 for JPY, 2 for GBP, 3 for KWD, 4 for CLF.
- **One way in.** `normalise(amount, currency)` is the only way untrusted input becomes `Money`.
  - It accepts plain decimal strings, ints (whole major units) and finite `Decimal`s.
  - It rejects floats, booleans, NaN/Infinity, exponent notation, locale formatting and non-ASCII
    digits. Python's `Decimal` accepts Arabic-Indic and full-width digits; a ledger should not.
- **Never round.** Digits beyond the currency's minor unit fail with `MONEY_PRECISION_EXCEEDED`.
  Trailing zeros are fine (`"12.340"` GBP is 1234 pence).
- **Typed errors.** Every rejection is a `MoneyError` with a stable `code` and structured `details`.
  The API passes these through unchanged.
- **Storage.** PostgreSQL stores `NUMERIC(38, 0)` minor units. Rejecting anything wider keeps
  sums exact in the database as well.

## What the property tests found

The first implementation converted with Decimal arithmetic:

```python
scaled = value * (10**exponent)
if scaled != scaled.to_integral_value():
    raise MoneyError(MONEY_PRECISION_EXCEEDED, ...)
return int(scaled)
```

The Hypothesis round-trip properties (`format()` then `normalise()` must return the same `Money`, for
every transactable currency and every amount up to 38 digits) failed 5 of 8. Arithmetic runs in the
active decimal context, which keeps **28 significant digits** by default:

- `10000000000000000000000000001` (29 digits) came back as `10000000000000000000000000000`.
- `131065118096312236.43000000001` AED should have been rejected for having too many decimal places.
  Instead the multiplication rounded away the extra digits, and the value was accepted.

That second case is worse than the first: the rounding happened *before* the precision check, so the
check passed.

Both are in commit `f3c41e5`. The fix, in `0a54e1f`, does no decimal arithmetic at all:

1. Read the sign, digit string and exponent from `Decimal.as_tuple()`. Constructing a `Decimal` from a
   string is exact; only arithmetic rounds.
2. Strip trailing zeros. Fractional digits left past the minor unit mean the amount needs rounding,
   so it is rejected.
3. Check the magnitude (number of digits) against the 38-digit limit *before* building an integer,
   so a hostile `1E+999999` costs nothing.
4. Compute `int(digits) * 10**shift`, which is exact in Python at any width.

Formatting goes the other way without a context: `divmod` on the integer, or the tuple constructor
`Decimal((sign, digits, -exponent))`. `Decimal.scaleb()` is avoided because it rounds to the context
as well.

## Consequences

- No amount anywhere in the system is a float, including the audit records and the API.
  Canonical JSON refuses floats outright.
- Clients must send amounts as strings. A JSON number with a fraction is rejected rather than
  trusted, because it has usually been through a binary float on the client already.
- The regression tests pin both falsifying examples, so the bug cannot quietly return.
