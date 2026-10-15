"""Load generator: decision latency against the budget, under concurrency.

In-process mode drives ``DecisionService`` directly. Python threads share one interpreter
lock, so concurrency comes from ``--processes`` (one service and pool per process, as with
several API workers or replicas against one PostgreSQL); ``--workers`` adds threads per
process. HTTP mode drives a running API over keep-alive connections, so it also pays for
JSON, ASGI and the network.

    SENTINEL_DATABASE_URL=postgresql://... python scripts/loadtest.py --processes 4
    python scripts/loadtest.py --url http://127.0.0.1:8000 --workers 8
"""

from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing
import os
import random
import statistics
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

CURRENCIES = (("GBP", 2, 70), ("EUR", 2, 15), ("USD", 2, 10), ("JPY", 0, 5))
CATEGORIES = ("5411", "5812", "5999", "4111", "5732", "5311", "7995")
COUNTRIES = ("GB", "GB", "GB", "GB", "FR", "DE", "US", "IE", "KP")


def synthetic_events(count: int, seed: int) -> list[dict[str, Any]]:
    """Deterministic, roughly card-shaped traffic: mostly small domestic payments."""
    rng = random.Random(seed)
    accounts = [f"acc_{i:05d}" for i in range(5_000)]
    merchants = [
        {
            "id": f"m_{i:04d}",
            "category_code": rng.choice(CATEGORIES[:-1]) if i % 97 else "7995",
            "country": rng.choice(COUNTRIES[:-1]) if i % 211 else "KP",
        }
        for i in range(800)
    ]
    occurred = (datetime.now(UTC) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    events = []
    for _ in range(count):
        code, exponent, _weight = rng.choices(CURRENCIES, weights=[c[2] for c in CURRENCIES])[0]
        bucket = rng.random()
        if bucket < 0.85:
            minor = rng.randint(100, 8_000)
        elif bucket < 0.98:
            minor = rng.randint(8_000, 150_000)
        else:
            minor = rng.randint(150_000, 2_000_000)
        amount = str(minor) if exponent == 0 else f"{minor // 100}.{minor % 100:02d}"
        events.append(
            {
                "event_id": f"load_{uuid4().hex}",
                "occurred_at": occurred,
                "account": {"id": rng.choice(accounts), "country": "GB"},
                "amount": amount,
                "currency": code,
                "channel": rng.choice(("card_present", "card_present", "ecommerce", "atm")),
                "merchant": rng.choice(merchants),
            }
        )
    return events


def percentiles(samples: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples, n=1000, method="inclusive")
    return {
        "p50": cuts[499],
        "p95": cuts[949],
        "p99": cuts[989],
        "p99.9": cuts[998],
        "max": max(samples),
    }


type Submit = Callable[[dict[str, Any]], tuple[str, int | None, bool]]


def drive(submit: Submit, events: list[dict[str, Any]], workers: int) -> dict[str, Any]:
    """Submit every event from ``workers`` threads; return the raw samples."""
    samples: dict[str, Any] = {
        "e2e_ms": [],
        "decision_ms": [],
        "outcomes": Counter(),
        "exhausted": 0,
    }
    lock = threading.Lock()

    def one(event: dict[str, Any]) -> None:
        started = time.perf_counter()
        outcome, decision_us, budget_exhausted = submit(event)
        elapsed = (time.perf_counter() - started) * 1000
        with lock:
            samples["e2e_ms"].append(elapsed)
            samples["outcomes"][outcome] += 1
            samples["exhausted"] += budget_exhausted
            if decision_us is not None:
                samples["decision_ms"].append(decision_us / 1000)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(one, events))
    return samples


def summarise(parts: list[dict[str, Any]], wall_s: float) -> dict[str, Any]:
    e2e = [x for part in parts for x in part["e2e_ms"]]
    decision = [x for part in parts for x in part["decision_ms"]]
    outcomes: Counter[str] = sum((part["outcomes"] for part in parts), Counter())
    summary: dict[str, Any] = {
        "events": len(e2e),
        "throughput_per_s": round(len(e2e) / wall_s),
        "end_to_end_ms": {k: round(v, 2) for k, v in percentiles(e2e).items()},
        "outcomes": dict(sorted(outcomes.items())),
        "budget_exhausted": sum(part["exhausted"] for part in parts),
    }
    if decision:
        summary["decision_ms"] = {k: round(v, 2) for k, v in percentiles(decision).items()}
    return summary


def in_process(args: argparse.Namespace) -> Submit:
    from sentinel.rules import default_ruleset_path, load_ruleset_file
    from sentinel.service import DecisionService
    from sentinel.store.db import create_pool

    pool = create_pool(
        os.environ["SENTINEL_DATABASE_URL"], min_size=args.workers, max_size=args.workers + 2
    )
    ruleset = load_ruleset_file(default_ruleset_path())
    service = DecisionService(pool, ruleset, budget_ms=args.budget_ms)
    service.register_ruleset()

    def submit(event: dict[str, Any]) -> tuple[str, int | None, bool]:
        decision = service.submit(event).decision
        return decision.outcome.value, decision.decision_latency_us, decision.budget_exhausted

    return submit


def over_http(args: argparse.Namespace) -> Submit:
    target = urllib.parse.urlsplit(args.url)
    local = threading.local()

    def submit(event: dict[str, Any]) -> tuple[str, int | None, bool]:
        if not hasattr(local, "conn"):
            local.conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
        local.conn.request(
            "POST",
            "/v1/transactions",
            body=json.dumps(event),
            headers={"Content-Type": "application/json"},
        )
        response = local.conn.getresponse()
        body = json.loads(response.read())
        if response.status not in (200, 201):
            raise RuntimeError(f"{response.status}: {body}")
        return body["outcome"], body["decision_latency_us"], body["budget_exhausted"]

    return submit


def worker_process(job: tuple[argparse.Namespace, list[dict[str, Any]], int]) -> dict[str, Any]:
    args, events, warmup = job
    submit = over_http(args) if args.url else in_process(args)
    drive(submit, events[:warmup], args.workers)
    samples = drive(submit, events[warmup:], args.workers)
    samples["outcomes"] = dict(samples["outcomes"])
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--events", type=int, default=20_000, help="measured events in total")
    parser.add_argument("--processes", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1, help="threads per process")
    parser.add_argument("--budget-ms", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=500, help="unmeasured events per process")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--url", help="drive a running API instead of the in-process service")
    args = parser.parse_args()

    share = args.events // args.processes
    jobs = [
        (args, synthetic_events(share + args.warmup, args.seed + index), args.warmup)
        for index in range(args.processes)
    ]
    started = time.perf_counter()
    with multiprocessing.get_context("spawn").Pool(args.processes) as pool:
        parts = pool.map(worker_process, jobs)
    wall = time.perf_counter() - started
    for part in parts:
        part["outcomes"] = Counter(part["outcomes"])
    summary = summarise(parts, wall)
    summary.update(
        mode="http" if args.url else "in-process",
        processes=args.processes,
        workers_per_process=args.workers,
        budget_ms=args.budget_ms,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
