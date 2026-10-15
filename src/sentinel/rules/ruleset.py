"""Rulesets: versioned, validated rule configuration.

A ruleset's version is the SHA-256 of its canonical JSON, so the version *is* the content:
two deployments with the same version run the same rules, and every decision records the
version it was made under. Configuration errors fail at startup with ``RULESET_INVALID``
and the JSON path of the problem, never at decision time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import reduce
from importlib import resources
from pathlib import Path
from typing import Final

from sentinel.canonical import canonical_json, digest
from sentinel.domain import Channel, Outcome
from sentinel.errors import ConfigurationError, ErrorCode, MoneyError, preview
from sentinel.money import Money, currency, normalise
from sentinel.rules.features import FeatureRequest
from sentinel.rules.rules import (
    AmountAbove,
    AttemptVelocity,
    CrossBorder,
    MerchantCategory,
    MerchantCountry,
    NewMerchantAmount,
    Rule,
    SpendVelocity,
)

_RULE_ID: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_COUNTRY: Final = re.compile(r"[A-Z]{2}")
_CATEGORY_CODE: Final = re.compile(r"[0-9]{4}")
_MAX_WINDOW_SECONDS: Final = 90 * 24 * 3600


@dataclass(frozen=True, slots=True)
class Ruleset:
    name: str
    version: str
    fallback_outcome: Outcome
    rules: tuple[Rule, ...]
    config: Mapping[str, object]

    @property
    def requires(self) -> FeatureRequest:
        return reduce(lambda acc, rule: acc | rule.requires, self.rules, FeatureRequest())


def _invalid(path: str, message: str, **details: str | int) -> ConfigurationError:
    return ConfigurationError(
        ErrorCode.RULESET_INVALID, f"{path}: {message}", details={"path": path, **details}
    )


class _Spec:
    """A JSON object in the config, with its path and the keys it is allowed to have."""

    def __init__(self, data: object, path: str, required: set[str], optional: set[str]) -> None:
        if not isinstance(data, dict):
            raise _invalid(path, "must be an object")
        missing = sorted(required - data.keys())
        if missing:
            raise _invalid(path, f"missing key(s): {', '.join(missing)}")
        unknown = sorted(data.keys() - required - optional)
        if unknown:
            raise _invalid(path, f"unknown key(s): {', '.join(unknown)}")
        self.data: dict[str, object] = data
        self.path = path

    def at(self, key: str) -> str:
        return f"{self.path}.{key}"

    def string(self, key: str, pattern: re.Pattern[str] | None = None) -> str:
        value = self.data[key]
        if not isinstance(value, str) or (pattern is not None and not pattern.fullmatch(value)):
            raise _invalid(self.at(key), f"invalid value {preview(value)}")
        return value

    def positive_int(self, key: str, maximum: int) -> int:
        value = self.data[key]
        if type(value) is not int or not 0 < value <= maximum:
            raise _invalid(self.at(key), f"must be an integer between 1 and {maximum}")
        return value

    def outcome(self, key: str = "outcome") -> Outcome:
        value = self.data[key]
        if not isinstance(value, str) or value not in Outcome.__members__:
            raise _invalid(self.at(key), f"must be one of {', '.join(Outcome)}")
        return Outcome(value)

    def string_set(self, key: str, pattern: re.Pattern[str]) -> frozenset[str]:
        value = self.data[key]
        if not isinstance(value, list) or not value:
            raise _invalid(self.at(key), "must be a non-empty list")
        for index, item in enumerate(value):
            if not isinstance(item, str) or not pattern.fullmatch(item):
                raise _invalid(f"{self.at(key)}[{index}]", f"invalid value {preview(item)}")
        return frozenset(value)

    def limits(self, key: str = "limits") -> dict[str, Money]:
        value = self.data[key]
        if not isinstance(value, dict) or not value:
            raise _invalid(self.at(key), "must be a non-empty object of currency -> amount")
        limits: dict[str, Money] = {}
        for code, amount in value.items():
            path = f"{self.at(key)}.{code}"
            try:
                limit = normalise(amount, currency(code))
            except MoneyError as error:
                raise _invalid(path, error.message, cause=error.code.value) from error
            if not limit.is_positive():
                raise _invalid(path, "limits must be positive")
            limits[code] = limit
        return limits


def _amount_above(spec: _Spec) -> Rule:
    return AmountAbove(spec.string("id"), spec.outcome(), spec.limits())


def _merchant_category(spec: _Spec) -> Rule:
    return MerchantCategory(
        spec.string("id"), spec.outcome(), spec.string_set("categories", _CATEGORY_CODE)
    )


def _merchant_country(spec: _Spec) -> Rule:
    return MerchantCountry(
        spec.string("id"), spec.outcome(), spec.string_set("countries", _COUNTRY)
    )


def _attempt_velocity(spec: _Spec) -> Rule:
    return AttemptVelocity(
        spec.string("id"),
        spec.outcome(),
        spec.positive_int("window_seconds", _MAX_WINDOW_SECONDS),
        spec.positive_int("max_prior_attempts", 1_000_000),
    )


def _spend_velocity(spec: _Spec) -> Rule:
    return SpendVelocity(
        spec.string("id"),
        spec.outcome(),
        spec.positive_int("window_seconds", _MAX_WINDOW_SECONDS),
        spec.limits(),
    )


def _new_merchant_amount(spec: _Spec) -> Rule:
    return NewMerchantAmount(spec.string("id"), spec.outcome(), spec.limits())


def _cross_border(spec: _Spec) -> Rule:
    channels = spec.string_set("channels", re.compile("|".join(c.value for c in Channel)))
    return CrossBorder(spec.string("id"), spec.outcome(), frozenset(Channel(c) for c in channels))


_BASE_KEYS: Final = {"id", "type", "outcome"}
_RULE_TYPES: Final[Mapping[str, tuple[set[str], Callable[[_Spec], Rule]]]] = {
    "amount_above": ({"limits"}, _amount_above),
    "merchant_category": ({"categories"}, _merchant_category),
    "merchant_country": ({"countries"}, _merchant_country),
    "attempt_velocity": ({"window_seconds", "max_prior_attempts"}, _attempt_velocity),
    "spend_velocity": ({"window_seconds", "limits"}, _spend_velocity),
    "new_merchant_amount": ({"limits"}, _new_merchant_amount),
    "cross_border": ({"channels"}, _cross_border),
}


def load_ruleset(config: object) -> Ruleset:
    root = _Spec(config, "$", {"name", "fallback_outcome", "rules"}, {"description"})
    name = root.string("name")
    fallback = root.outcome("fallback_outcome")
    raw_rules = root.data["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _invalid("$.rules", "must be a non-empty list")

    rules: list[Rule] = []
    for index, raw in enumerate(raw_rules):
        path = f"$.rules[{index}]"
        rule_type = raw.get("type") if isinstance(raw, dict) else None
        if not isinstance(rule_type, str) or rule_type not in _RULE_TYPES:
            raise _invalid(f"{path}.type", f"must be one of {', '.join(sorted(_RULE_TYPES))}")
        keys, build = _RULE_TYPES[rule_type]
        spec = _Spec(raw, path, _BASE_KEYS | keys, set())
        spec.string("id", _RULE_ID)
        rule = build(spec)
        if any(existing.id == rule.id for existing in rules):
            raise _invalid(f"{path}.id", f"duplicate rule id {rule.id!r}")
        rules.append(rule)

    try:
        version = digest(config)
    except TypeError as error:  # floats in the config file
        raise _invalid("$", str(error)) from error
    canonical: dict[str, object] = json.loads(canonical_json(config))
    return Ruleset(name, version, fallback, tuple(rules), canonical)


def load_ruleset_file(path: str | Path) -> Ruleset:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            ErrorCode.RULESET_INVALID,
            f"cannot read ruleset: {error.strerror}",
            details={"path": str(path)},
        ) from error
    try:
        config = json.loads(text)
    except ValueError as error:
        raise ConfigurationError(
            ErrorCode.RULESET_INVALID, f"ruleset is not valid JSON: {error}", details={"path": "$"}
        ) from error
    return load_ruleset(config)


def default_ruleset_path() -> Path:
    return Path(str(resources.files("sentinel") / "rulesets" / "default.json"))
