"""Migrations and the command line, against the test database."""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest

from sentinel.cli import main
from sentinel.errors import ConfigurationError, ErrorCode
from sentinel.service import DecisionService
from sentinel.store.db import Pool
from sentinel.store.migrate import Migration, discover, migrate
from tests import support


@pytest.fixture
def scratch_schema(migrated_url: str) -> Iterator[str]:
    """An empty schema for migration tests, so the shared one stays intact."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS migration_test CASCADE")
        conn.execute("CREATE SCHEMA migration_test")
    yield f"{migrated_url}?options=-csearch_path%3Dmigration_test"
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA migration_test CASCADE")


def _tables(url: str) -> set[str]:
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    return {row[0] for row in rows}


def test_migrations_create_the_schema(scratch_schema: str) -> None:
    with psycopg.connect(scratch_schema) as conn:
        assert migrate(conn) == ["0001_initial"]
    assert _tables(scratch_schema) == {
        "schema_migrations",
        "rulesets",
        "transactions",
        "decisions",
        "decision_audit",
        "outbox",
    }


def test_migrations_are_idempotent(scratch_schema: str) -> None:
    with psycopg.connect(scratch_schema) as conn:
        migrate(conn)
        assert migrate(conn) == []


def test_concurrent_migrators_apply_each_migration_once(scratch_schema: str) -> None:
    def run(_: int) -> list[str]:
        with psycopg.connect(scratch_schema) as conn:
            return migrate(conn)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, range(4)))
    assert sorted(results) == [[], [], [], ["0001_initial"]]


def test_new_migrations_are_applied_in_order(scratch_schema: str) -> None:
    extra = Migration("0002", "add_note", "ALTER TABLE decisions ADD COLUMN note TEXT;")
    with psycopg.connect(scratch_schema) as conn:
        migrate(conn)
        assert migrate(conn, [*discover(), extra]) == ["0002_add_note"]


def test_an_edited_migration_is_refused(scratch_schema: str) -> None:
    with psycopg.connect(scratch_schema) as conn:
        migrate(conn)
        tampered = [Migration(m.version, m.name, m.sql + "\n-- edited") for m in discover()]
        with pytest.raises(ConfigurationError) as caught:
            migrate(conn, tampered)
    assert caught.value.code is ErrorCode.MIGRATION_CHECKSUM_MISMATCH


def _run(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], url: str
) -> tuple[int, str, str]:
    monkeypatch.setenv("SENTINEL_DATABASE_URL", url)
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_migrate_reports_nothing_pending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrated_url: str
) -> None:
    code, out, _ = _run(["migrate"], monkeypatch, capsys, migrated_url)
    assert code == 0
    assert json.loads(out) == {"applied": []}


def test_cli_verify_passes_for_untouched_decisions(
    service: DecisionService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
) -> None:
    decision = service.submit(support.event()).decision
    code, out, _ = _run(["verify"], monkeypatch, capsys, migrated_url)
    assert code == 0
    assert json.loads(out) == {"checked": 1, "failed": 0}
    code, out, _ = _run(["verify", str(decision.decision_id)], monkeypatch, capsys, migrated_url)
    assert code == 0


def test_cli_verify_fails_on_tampering(
    service: DecisionService,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
) -> None:
    service.submit(support.event())
    support.execute(pool, "UPDATE decisions SET reasons = ARRAY['forged']")
    code, out, _ = _run(["verify"], monkeypatch, capsys, migrated_url)
    lines = [json.loads(line) for line in out.splitlines()]
    assert code == 1
    assert lines[0]["replay_matches"] is False
    assert lines[-1] == {"checked": 1, "failed": 1}


def test_cli_relay_once_prints_events(
    service: DecisionService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
) -> None:
    service.submit(support.event())
    code, out, _ = _run(["relay", "--once"], monkeypatch, capsys, migrated_url)
    assert code == 0
    assert json.loads(out)["topic"] == "decision.made"


def test_cli_relay_webhook_needs_a_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], migrated_url: str
) -> None:
    code, _, err = _run(["relay", "--sink", "webhook"], monkeypatch, capsys, migrated_url)
    assert code == 2
    assert "--webhook-url" in err


def test_cli_ruleset_prints_the_version(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(support.ruleset(support.CEILING).config))
    assert main(["ruleset", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "name": "test",
        "version": support.ruleset(support.CEILING).version,
        "rules": 1,
    }


def test_cli_reports_configuration_errors_as_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTINEL_DATABASE_URL", raising=False)
    assert main(["migrate"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "SETTINGS_INVALID"
    assert main(["ruleset", "/nonexistent/rules.json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "RULESET_INVALID"
