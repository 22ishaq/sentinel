"""Minimal, thread-safe metrics with Prometheus text exposition.

Deliberately small: counters and fixed-bucket histograms are all the decision path needs,
and avoiding a client library keeps the hot path free of global registries and locks we
do not control. State is per process; run one scrape target per worker.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections.abc import Sequence
from typing import Final

type Labels = tuple[tuple[str, str], ...]

LATENCY_BUCKETS_S: Final = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)


def _labels(labels: dict[str, str] | None) -> Labels:
    return tuple(sorted((labels or {}).items()))


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: Labels) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_escape(value)}"' for key, value in labels) + "}"


class Counter:
    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._values: dict[Labels, float] = {}
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, labels: dict[str, str] | None = None) -> None:
        key = _labels(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._values.get(_labels(labels), 0.0)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            lines += [
                f"{self.name}{_render_labels(key)} {value:g}"
                for key, value in sorted(self._values.items())
            ]
        return lines


class Histogram:
    def __init__(self, name: str, help_text: str, buckets: Sequence[float]) -> None:
        self.name = name
        self.help_text = help_text
        self.buckets = tuple(sorted(buckets))
        self._counts = [0] * (len(self.buckets) + 1)
        self._sum = 0.0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        index = bisect_left(self.buckets, value)
        with self._lock:
            self._counts[index] += 1
            self._sum += value

    @property
    def count(self) -> int:
        with self._lock:
            return sum(self._counts)

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        with self._lock:
            cumulative = 0
            for bound, count in zip(self.buckets, self._counts, strict=False):
                cumulative += count
                lines.append(f'{self.name}_bucket{{le="{bound:g}"}} {cumulative}')
            cumulative += self._counts[-1]
            lines.append(f'{self.name}_bucket{{le="+Inf"}} {cumulative}')
            lines.append(f"{self.name}_sum {self._sum:.6f}")
            lines.append(f"{self.name}_count {cumulative}")
        return lines


class Metrics:
    """The service's metric set."""

    def __init__(self) -> None:
        self.decisions = Counter("sentinel_decisions_total", "Decisions made, by outcome.")
        self.replays = Counter(
            "sentinel_idempotent_replays_total", "Duplicate submissions answered from the store."
        )
        self.conflicts = Counter(
            "sentinel_idempotency_conflicts_total", "Event ids reused with a different payload."
        )
        self.degraded = Counter(
            "sentinel_degraded_decisions_total",
            "Decisions that fell back because evidence was not gathered in time, by reason.",
        )
        self.rejected = Counter("sentinel_rejected_events_total", "Invalid events, by error code.")
        self.decision_latency = Histogram(
            "sentinel_decision_latency_seconds",
            "Time from receipt to decision (validation, claim, features, rules).",
            LATENCY_BUCKETS_S,
        )
        self.request_latency = Histogram(
            "sentinel_submit_latency_seconds",
            "End-to-end submit latency including persistence and commit.",
            LATENCY_BUCKETS_S,
        )

    def render(self) -> str:
        lines: list[str] = []
        for metric in (
            self.decisions,
            self.replays,
            self.conflicts,
            self.degraded,
            self.rejected,
            self.decision_latency,
            self.request_latency,
        ):
            lines += metric.render()
        return "\n".join(lines) + "\n"
