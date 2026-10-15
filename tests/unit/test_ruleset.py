from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from sentinel.errors import ConfigurationError, ErrorCode
from sentinel.rules import default_ruleset_path, load_ruleset, load_ruleset_file
from tests import support


def _config(*rules: dict[str, Any]) -> dict[str, Any]:
    chosen = rules or (support.CEILING,)
    return {"name": "t", "fallback_outcome": "REVIEW", "rules": copy.deepcopy(list(chosen))}


def _invalid(config: object) -> ConfigurationError:
    with pytest.raises(ConfigurationError) as caught:
        load_ruleset(config)
    assert caught.value.code is ErrorCode.RULESET_INVALID
    return caught.value


def test_the_shipped_default_ruleset_loads() -> None:
    ruleset = load_ruleset_file(default_ruleset_path())
    assert ruleset.name == "default"
    assert len(ruleset.rules) == 8
    assert len(ruleset.version) == 64


def test_the_version_is_the_content_hash() -> None:
    config = _config(support.CEILING, support.HIGH_VALUE)
    shuffled = json.loads(json.dumps(config, sort_keys=True, indent=4))
    assert load_ruleset(config).version == load_ruleset(shuffled).version
    changed = copy.deepcopy(config)
    changed["rules"][1]["limits"]["GBP"] = "501.00"
    assert load_ruleset(changed).version != load_ruleset(config).version


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda c: c.pop("name"), "$"),
        (lambda c: c.update(extra=True), "$"),
        (lambda c: c.update(fallback_outcome="MAYBE"), "$.fallback_outcome"),
        (lambda c: c.update(rules=[]), "$.rules"),
        (lambda c: c["rules"][0].update(type="magic"), "$.rules[0].type"),
        (lambda c: c["rules"][0].update(id="Bad Id"), "$.rules[0].id"),
        (lambda c: c["rules"][0].update(outcome="approve"), "$.rules[0].outcome"),
        (lambda c: c["rules"][0].update(colour="red"), "$.rules[0]"),
        (lambda c: c["rules"][0].pop("limits"), "$.rules[0]"),
        (lambda c: c["rules"][0].update(limits={}), "$.rules[0].limits"),
        (lambda c: c["rules"][0].update(limits={"GBP": "10.001"}), "$.rules[0].limits.GBP"),
        (lambda c: c["rules"][0].update(limits={"GBP": "0"}), "$.rules[0].limits.GBP"),
        (lambda c: c["rules"][0].update(limits={"QQQ": "10"}), "$.rules[0].limits.QQQ"),
        (lambda c: c["rules"].append(dict(c["rules"][0])), "$.rules[1].id"),
        (lambda c: c["rules"].append("rule"), "$.rules[1].type"),
    ],
)
def test_invalid_configs_point_at_the_problem(mutate: Any, path: str) -> None:
    config = _config()
    mutate(config)
    assert _invalid(config).details["path"] == path


def test_money_problems_in_limits_keep_their_original_code() -> None:
    rule = {**support.CEILING, "limits": {"GBP": "1.234"}}
    assert _invalid(_config(rule)).details["cause"] == "MONEY_PRECISION_EXCEEDED"


@pytest.mark.parametrize(
    ("rule", "path"),
    [
        ({**support.RAPID_ATTEMPTS, "window_seconds": 0}, "$.rules[0].window_seconds"),
        ({**support.RAPID_ATTEMPTS, "window_seconds": True}, "$.rules[0].window_seconds"),
        ({**support.RAPID_ATTEMPTS, "max_prior_attempts": "3"}, "$.rules[0].max_prior_attempts"),
        ({**support.BLOCKED_COUNTRY, "countries": []}, "$.rules[0].countries"),
        ({**support.BLOCKED_COUNTRY, "countries": ["gb"]}, "$.rules[0].countries[0]"),
        ({**support.BLOCKED_CATEGORY, "categories": "7995"}, "$.rules[0].categories"),
        ({**support.CROSS_BORDER, "channels": ["teleport"]}, "$.rules[0].channels[0]"),
    ],
)
def test_rule_parameters_are_validated(rule: dict[str, Any], path: str) -> None:
    assert _invalid(_config(rule)).details["path"] == path


def test_floats_in_a_config_are_rejected() -> None:
    config = _config()
    config["description"] = 0.5
    assert "float" in _invalid(config).message


def test_unreadable_files_and_bad_json_are_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError) as missing:
        load_ruleset_file(tmp_path / "missing.json")
    assert missing.value.code is ErrorCode.RULESET_INVALID
    broken = tmp_path / "broken.json"
    broken.write_text("{nope")
    with pytest.raises(ConfigurationError):
        load_ruleset_file(broken)
