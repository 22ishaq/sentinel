"""PostgreSQL connection pool."""

from __future__ import annotations

from psycopg import Connection
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

type Conn = Connection[TupleRow]
type Pool = ConnectionPool[Connection[TupleRow]]


def create_pool(
    conninfo: str,
    *,
    min_size: int = 2,
    max_size: int = 20,
    acquire_timeout_s: float = 5.0,
    statement_timeout_ms: int = 5_000,
    open_timeout_s: float = 10.0,
) -> Pool:
    """Open a pool and wait until ``min_size`` connections are established.

    ``statement_timeout`` is the session default; decision-path queries tighten it per
    transaction to whatever remains of the latency budget.
    """
    pool: Pool = ConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        timeout=acquire_timeout_s,
        open=False,
        kwargs={
            "application_name": "sentinel",
            "options": f"-c statement_timeout={statement_timeout_ms}",
        },
    )
    pool.open(wait=True, timeout=open_timeout_s)
    return pool
