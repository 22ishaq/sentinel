"""Canonical JSON: exactly one byte sequence per value, so digests are stable.

Fingerprints (idempotency), ruleset versions and audit digests are all SHA-256 over this
encoding. Floats are refused outright: their text form differs between languages and
runtimes, which would make a digest computed elsewhere impossible to reproduce.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime


def _check(value: object, path: str) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        raise TypeError(f"floats have no canonical encoding (at {path})")
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"object keys must be strings (at {path})")
            _check(item, f"{path}.{key}")
        return
    raise TypeError(f"{type(value).__name__} is not JSON (at {path})")


def canonical_json(value: object) -> bytes:
    """Sorted keys, no insignificant whitespace, UTF-8, integers only."""
    _check(value, "$")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value: object) -> str:
    """SHA-256 of the canonical encoding, as lowercase hex."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def format_timestamp(moment: datetime) -> str:
    """RFC 3339 in UTC with fixed microsecond precision, e.g. ``2026-10-15T09:30:00.000000Z``."""
    if moment.tzinfo is None:
        raise ValueError("refusing to format a naive datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
