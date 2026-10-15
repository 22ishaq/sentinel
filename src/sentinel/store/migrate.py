"""Forward-only SQL migrations, shipped inside the package.

Pending migrations are applied in one transaction under an advisory lock, so two replicas
starting together cannot race. The checksum of every applied file is recorded; editing a
migration after it has run is refused instead of leaving schemas that silently differ.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources
from typing import Final

from sentinel.errors import ConfigurationError, ErrorCode
from sentinel.store.db import Conn

MIGRATION_LOCK_ID: Final = 0x53454E54  # "SENT"
_FILENAME: Final = re.compile(r"(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql")


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover() -> list[Migration]:
    folder = resources.files("sentinel.store") / "migrations"
    found = []
    for entry in folder.iterdir():
        match = _FILENAME.fullmatch(entry.name)
        if match:
            found.append(
                Migration(match["version"], match["name"], entry.read_text(encoding="utf-8"))
            )
    return sorted(found, key=lambda migration: migration.version)


def migrate(conn: Conn, migrations: Sequence[Migration] | None = None) -> list[str]:
    """Apply pending migrations; return the ones applied by this call."""
    pending = discover() if migrations is None else list(migrations)
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version TEXT PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        recorded: dict[str, str] = dict(
            conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
        )
        for migration in pending:
            label = f"{migration.version}_{migration.name}"
            checksum = recorded.get(migration.version)
            if checksum is not None:
                if checksum != migration.checksum:
                    raise ConfigurationError(
                        ErrorCode.MIGRATION_CHECKSUM_MISMATCH,
                        f"migration {label} was edited after it was applied",
                        details={"version": migration.version},
                    )
                continue
            conn.execute(migration.sql.encode("utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
            applied_now.append(label)
    return applied_now
