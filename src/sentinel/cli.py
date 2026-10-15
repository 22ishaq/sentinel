"""Command line: ``sentinel migrate | serve | relay | verify | ruleset``."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from collections.abc import Sequence
from uuid import UUID

import psycopg

from sentinel import __version__
from sentinel.errors import SentinelError
from sentinel.logs import configure_logging
from sentinel.rules import load_ruleset_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel", description="Real-time transaction decisioning."
    )
    parser.add_argument("--version", action="version", version=f"sentinel {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("migrate", help="apply pending database migrations")

    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", help="bind address (default: SENTINEL_HOST or 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default: SENTINEL_PORT or 8000)")

    relay = commands.add_parser("relay", help="publish outbox events")
    relay.add_argument("--sink", choices=["stdout", "webhook"], default="stdout")
    relay.add_argument("--webhook-url", help="target URL for --sink webhook")
    relay.add_argument("--once", action="store_true", help="publish one batch and exit")

    verify = commands.add_parser("verify", help="re-derive decisions from their audit records")
    verify.add_argument("decision_id", nargs="?", type=UUID, help="one decision (default: all)")

    ruleset = commands.add_parser("ruleset", help="validate a ruleset and print its version")
    ruleset.add_argument("path", nargs="?", help="ruleset JSON (default: SENTINEL_RULESET)")
    return parser


def _migrate() -> int:
    from sentinel.config import Settings
    from sentinel.store.migrate import migrate

    settings = Settings.from_env()
    with psycopg.connect(settings.require_database_url()) as conn:
        applied = migrate(conn)
    print(json.dumps({"applied": applied}))
    return 0


def _serve(host: str | None, port: int | None) -> int:
    import uvicorn

    from sentinel.api import create_app, pool_lifespan
    from sentinel.config import Settings
    from sentinel.service import DecisionService
    from sentinel.store.db import create_pool

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    ruleset = load_ruleset_file(settings.ruleset_path)
    pool = create_pool(
        settings.require_database_url(),
        min_size=settings.pool_min_size,
        max_size=settings.pool_max_size,
    )
    service = DecisionService(pool, ruleset, budget_ms=settings.budget_ms)
    service.register_ruleset()
    app = create_app(service, lifespan=pool_lifespan(pool.close))
    uvicorn.run(
        app,
        host=host or settings.host,
        port=port or settings.port,
        log_config=None,
        access_log=False,
    )
    return 0


def _relay(sink_name: str, webhook_url: str | None, once: bool) -> int:
    from sentinel.config import Settings
    from sentinel.outbox import OutboxRelay, Sink, StdoutSink, WebhookSink
    from sentinel.store.db import create_pool

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if sink_name == "webhook" and not webhook_url:
        print("--webhook-url is required with --sink webhook", file=sys.stderr)
        return 2
    sink: Sink = WebhookSink(webhook_url) if webhook_url else StdoutSink()
    conninfo = settings.require_database_url()
    pool = create_pool(conninfo, min_size=1, max_size=2)
    try:
        relay = OutboxRelay(pool, sink)
        if once:
            relay.run_once()
            return 0
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        relay.run_forever(conninfo, stop)
    finally:
        pool.close()
    return 0


def _verify(decision_id: UUID | None) -> int:
    from sentinel.config import Settings
    from sentinel.service import DecisionService
    from sentinel.store.db import create_pool

    settings = Settings.from_env()
    pool = create_pool(settings.require_database_url(), min_size=1, max_size=2)
    try:
        service = DecisionService(pool, load_ruleset_file(settings.ruleset_path))
        results = (
            [service.audit_report(decision_id).verification]
            if decision_id
            else service.verify_all()
        )
        checked = failed = 0
        for verification in results:
            checked += 1
            if not verification.ok:
                failed += 1
                print(json.dumps(verification.to_json()))
        print(json.dumps({"checked": checked, "failed": failed}))
    finally:
        pool.close()
    return 1 if failed else 0


def _ruleset(path: str | None) -> int:
    from sentinel.config import Settings

    ruleset = load_ruleset_file(path or Settings.from_env().ruleset_path)
    print(
        json.dumps({"name": ruleset.name, "version": ruleset.version, "rules": len(ruleset.rules)})
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        match args.command:
            case "migrate":
                return _migrate()
            case "serve":
                return _serve(args.host, args.port)
            case "relay":
                return _relay(args.sink, args.webhook_url, args.once)
            case "verify":
                return _verify(args.decision_id)
            case _:
                return _ruleset(args.path)
    except SentinelError as error:
        print(json.dumps({"error": error.to_dict()}), file=sys.stderr)
        return 2
