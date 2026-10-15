"""Settings from environment variables (twelve-factor), validated once at startup."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sentinel.errors import ConfigurationError, ErrorCode
from sentinel.rules import default_ruleset_path


def _int(environ: Mapping[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    if not raw.isdigit() or int(raw) < minimum:
        raise ConfigurationError(
            ErrorCode.SETTINGS_INVALID,
            f"{name} must be an integer >= {minimum}",
            details={"variable": name},
        )
    return int(raw)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str | None
    ruleset_path: Path
    budget_ms: int
    pool_min_size: int
    pool_max_size: int
    host: str
    port: int
    log_level: str

    def require_database_url(self) -> str:
        if not self.database_url:
            raise ConfigurationError(
                ErrorCode.SETTINGS_INVALID,
                "SENTINEL_DATABASE_URL is not set",
                details={"variable": "SENTINEL_DATABASE_URL"},
            )
        return self.database_url

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        ruleset = env.get("SENTINEL_RULESET")
        settings = cls(
            database_url=env.get("SENTINEL_DATABASE_URL") or None,
            ruleset_path=Path(ruleset) if ruleset else default_ruleset_path(),
            budget_ms=_int(env, "SENTINEL_DECISION_BUDGET_MS", 50),
            pool_min_size=_int(env, "SENTINEL_POOL_MIN_SIZE", 2),
            pool_max_size=_int(env, "SENTINEL_POOL_MAX_SIZE", 20),
            host=env.get("SENTINEL_HOST", "127.0.0.1"),
            port=_int(env, "SENTINEL_PORT", 8000),
            log_level=env.get("SENTINEL_LOG_LEVEL", "INFO").upper(),
        )
        if settings.pool_min_size > settings.pool_max_size:
            raise ConfigurationError(
                ErrorCode.SETTINGS_INVALID,
                "SENTINEL_POOL_MIN_SIZE must not exceed SENTINEL_POOL_MAX_SIZE",
                details={"variable": "SENTINEL_POOL_MIN_SIZE"},
            )
        return settings
