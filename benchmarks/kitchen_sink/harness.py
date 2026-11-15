"""Timing, statement capture and profile attribution shared by every backend."""

from __future__ import annotations

import cProfile
import hashlib
import json
import pstats
import statistics
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class SqlRecorder:
    """Counts statements and the time spent inside the driver's execute calls."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self, keep_statements: bool = False) -> None:
        with self.lock:
            self.count = 0
            self.seconds = 0.0
            self.statements = []
            self.params = []
            self.keep_statements = keep_statements
            self.in_flight = 0
            self.busy_since = 0.0

    def begin(self, sql: str) -> None:
        """A statement starts; overlapping statements count their union once."""
        with self.lock:
            self.count += 1
            if self.keep_statements:
                self.statements.append(sql)
            if self.in_flight == 0:
                self.busy_since = time.perf_counter()
            self.in_flight += 1

    def end(self) -> None:
        with self.lock:
            self.in_flight -= 1
            if self.in_flight == 0:
                self.seconds += time.perf_counter() - self.busy_since

    def add(self, sql: str, seconds: float) -> None:
        with self.lock:
            self.count += 1
            self.seconds += seconds
            if self.keep_statements:
                self.statements.append(sql)

    def add_time(self, seconds: float) -> None:
        """Driver time that is not a statement of its own (fetching rows)."""
        with self.lock:
            self.seconds += seconds


def timed_sqlite_cursor(base: type) -> type:
    """A subclass of a ``sqlite3.Cursor`` class timing executes and fetches.

    SQLite does most of a query's work while rows are stepped through, so the
    fetches count as database time too.
    """

    class TimedCursor(base):
        def execute(self, sql, *args):
            t0 = time.perf_counter()
            try:
                return super().execute(sql, *args)
            finally:
                RECORDER.add(sql, time.perf_counter() - t0)
                if RECORDER.keep_statements:
                    RECORDER.params.append(args[0] if args else ())

        def executemany(self, sql, *args):
            t0 = time.perf_counter()
            try:
                return super().executemany(sql, *args)
            finally:
                RECORDER.add(sql, time.perf_counter() - t0)

        def fetchone(self):
            t0 = time.perf_counter()
            try:
                return super().fetchone()
            finally:
                RECORDER.add_time(time.perf_counter() - t0)

        def fetchmany(self, *args):
            t0 = time.perf_counter()
            try:
                return super().fetchmany(*args)
            finally:
                RECORDER.add_time(time.perf_counter() - t0)

        def fetchall(self):
            t0 = time.perf_counter()
            try:
                return super().fetchall()
            finally:
                RECORDER.add_time(time.perf_counter() - t0)

    TimedCursor.__name__ = f"Timed{base.__name__}"
    return TimedCursor


RECORDER = SqlRecorder()


@dataclass
class Sample:
    wall_ms: list[float] = field(default_factory=list)
    sql_ms: list[float] = field(default_factory=list)
    statements: int = 0

    def summary(self) -> dict[str, Any]:
        wall = sorted(self.wall_ms)
        sql = sorted(self.sql_ms)
        median = statistics.median(wall)
        sql_median = statistics.median(sql)
        return {
            "runs": len(wall),
            "median_ms": round(median, 2),
            "min_ms": round(wall[0], 2),
            "p90_ms": round(wall[int(0.9 * (len(wall) - 1))], 2),
            "sql_ms": round(sql_median, 2),
            "python_ms": round(median - sql_median, 2),
            "statements": self.statements,
        }


def fingerprint(data: Any) -> str:
    text = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def _runs_for(first_ms: float, budget_ms: float, min_runs: int, max_runs: int) -> int:
    return max(min_runs, min(max_runs, int(budget_ms / max(first_ms, 0.1))))


def bench_sync(
    run: Callable[[], Any],
    *,
    warmup: int = 2,
    min_runs: int = 5,
    max_runs: int = 30,
    budget_ms: float = 3000,
) -> tuple[Sample, Any]:
    data = None
    for _ in range(warmup):
        data = run()
    RECORDER.reset()
    t0 = time.perf_counter()
    run()
    first = (time.perf_counter() - t0) * 1000
    sample = Sample(statements=RECORDER.count)
    for _ in range(_runs_for(first, budget_ms, min_runs, max_runs)):
        RECORDER.reset()
        t0 = time.perf_counter()
        run()
        sample.wall_ms.append((time.perf_counter() - t0) * 1000)
        sample.sql_ms.append(RECORDER.seconds * 1000)
    return sample, data


async def bench_async(
    run: Callable[[], Awaitable[Any]],
    *,
    warmup: int = 2,
    min_runs: int = 5,
    max_runs: int = 30,
    budget_ms: float = 3000,
) -> tuple[Sample, Any]:
    data = None
    for _ in range(warmup):
        data = await run()
    RECORDER.reset()
    t0 = time.perf_counter()
    await run()
    first = (time.perf_counter() - t0) * 1000
    sample = Sample(statements=RECORDER.count)
    for _ in range(_runs_for(first, budget_ms, min_runs, max_runs)):
        RECORDER.reset()
        t0 = time.perf_counter()
        await run()
        sample.wall_ms.append((time.perf_counter() - t0) * 1000)
        sample.sql_ms.append(RECORDER.seconds * 1000)
    return sample, data


# -- profile attribution ------------------------------------------------------

LAYERS = [
    ("strawberry_orm", ("/strawberry_orm/",)),
    ("graphql-core", ("/graphql/",)),
    ("strawberry", ("/strawberry/",)),
    ("django", ("/django/",)),
    ("sqlalchemy", ("/sqlalchemy/",)),
    ("tortoise", ("/tortoise/", "/pypika_tortoise/")),
    ("asgiref", ("/asgiref/",)),
    ("asyncio/threads", ("/asyncio/", "/concurrent/", "threading.py", "selectors.py")),
    ("aiosqlite", ("/aiosqlite/",)),
]


def layer_of(filename: str, funcname: str) -> str:
    if "sqlite3" in funcname or "sqlite3" in filename:
        return "sqlite3 driver"
    for name, needles in LAYERS:
        if any(needle in filename for needle in needles):
            return name
    if filename == "~":
        if "select" in funcname or "kqueue" in funcname or "control" in funcname:
            return "asyncio/threads"
        if "acquire" in funcname or "wait" in funcname:
            return "asyncio/threads"
        return "builtins"
    return "stdlib/other"


def profile_layers(stats: pstats.Stats) -> dict[str, float]:
    by_layer: dict[str, float] = {}
    total = 0.0
    for (filename, _line, funcname), (
        _cc,
        _nc,
        tt,
        _ct,
        _callers,
    ) in stats.stats.items():  # type: ignore[attr-defined]
        layer = layer_of(filename, funcname)
        by_layer[layer] = by_layer.get(layer, 0.0) + tt
        total += tt
    return {
        k: round(100 * v / total, 1)
        for k, v in sorted(by_layer.items(), key=lambda kv: -kv[1])
    }


def top_functions(
    stats: pstats.Stats, needle: str, key: int, limit: int = 15
) -> list[dict]:
    """Functions whose file contains *needle*, by tottime (key=2) or cumtime (key=3)."""
    rows = []
    total = sum(v[2] for v in stats.stats.values())  # type: ignore[attr-defined]
    for (filename, line, funcname), values in stats.stats.items():  # type: ignore[attr-defined]
        if needle and needle not in filename:
            continue
        rows.append(
            {
                "function": f"{filename.split('/src/')[-1].split('site-packages/')[-1]}:{line}({funcname})",
                "calls": values[1],
                "tottime_pct": round(100 * values[2] / total, 1),
                "cumtime_pct": round(100 * values[3] / total, 1),
            }
        )
    rows.sort(key=lambda r: -(r["tottime_pct"] if key == 2 else r["cumtime_pct"]))
    return rows[:limit]


def profile_sync(run: Callable[[], Any], runs: int, path: str) -> pstats.Stats:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(runs):
        run()
    profiler.disable()
    profiler.dump_stats(path)
    return pstats.Stats(profiler)


async def profile_async(
    run: Callable[[], Awaitable[Any]], runs: int, path: str
) -> pstats.Stats:
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(runs):
        await run()
    profiler.disable()
    profiler.dump_stats(path)
    return pstats.Stats(profiler)
