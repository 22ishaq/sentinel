from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from hypothesis import settings

from sentinel.rules import Ruleset
from sentinel.service import DecisionService
from sentinel.store.db import Pool, create_pool
from sentinel.store.migrate import migrate
from tests import support

settings.register_profile("dev", max_examples=200, deadline=None)
settings.register_profile("ci", max_examples=1000, deadline=None, print_blob=True)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))

INTEGRATION_DIR = Path(__file__).parent / "integration"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("SENTINEL_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("SENTINEL_REQUIRE_DB") == "1":
            pytest.fail("SENTINEL_TEST_DATABASE_URL must be set when SENTINEL_REQUIRE_DB=1")
        pytest.skip("SENTINEL_TEST_DATABASE_URL is not set")
    return url


@pytest.fixture(scope="session")
def migrated_url(database_url: str) -> str:
    """Reset the test database's schema once per session, then apply migrations."""
    with psycopg.connect(database_url, autocommit=True) as conn:
        if "test" not in conn.info.dbname:
            pytest.fail(
                f"refusing to reset {conn.info.dbname!r}: test database names contain 'test'"
            )
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    with psycopg.connect(database_url) as conn:
        migrate(conn)
    return database_url


@pytest.fixture(scope="session")
def session_pool(migrated_url: str) -> Iterator[Pool]:
    pool = create_pool(migrated_url, min_size=2, max_size=40)
    yield pool
    pool.close()


@pytest.fixture
def pool(session_pool: Pool) -> Pool:
    """The shared pool, with every table emptied before the test."""
    with session_pool.connection() as conn:
        conn.execute(
            "TRUNCATE outbox, decision_audit, decisions, transactions, rulesets RESTART IDENTITY"
        )
    return session_pool


@pytest.fixture
def make_service(pool: Pool) -> Callable[..., DecisionService]:
    def factory(
        *rules: dict[str, Any], ruleset: Ruleset | None = None, **kwargs: Any
    ) -> DecisionService:
        active = ruleset or support.ruleset(*(rules or support.ALL_RULES))
        kwargs.setdefault("budget_ms", 5_000)
        service = DecisionService(pool, active, **kwargs)
        service.register_ruleset()
        return service

    return factory


@pytest.fixture
def service(make_service: Callable[..., DecisionService]) -> DecisionService:
    return make_service()
