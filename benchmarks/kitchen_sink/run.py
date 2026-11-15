"""Run the kitchen-sink workload on one backend variant, or on all of them.

    .venv/bin/python -m benchmarks.kitchen_sink.run --variant all
    .venv/bin/python -m benchmarks.kitchen_sink.run --variant django --only deep_nested --profile
    .venv/bin/python -m benchmarks.kitchen_sink.run --variant sqlalchemy --show-sql deep_nested

Each variant runs in its own process: Django settings and Tortoise state are global.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import importlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

VARIANTS = {
    "django": ("django", "sync"),
    "django-async": ("django", "async"),
    "sqlalchemy": ("sqlalchemy", "sync"),
    "sqlalchemy-async": ("sqlalchemy", "async"),
    "tortoise": ("tortoise", "async"),
}


def _shape(data) -> dict:
    """Counts that should agree across backends: objects returned and totalCount."""
    objects = 0
    totals = []

    def walk(value):
        nonlocal objects
        if isinstance(value, dict):
            objects += 1
            if "totalCount" in value and isinstance(value["totalCount"], int):
                totals.append(value["totalCount"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return {"objects": objects, "total_count": totals[0] if totals else None}


def run_variant(args) -> dict:
    from benchmarks.kitchen_sink import current, harness
    from benchmarks.kitchen_sink.data import generate
    from benchmarks.kitchen_sink.queries import WORKLOADS

    backend_name, mode = VARIANTS[args.variant]
    data = generate(args.scale)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # Every coroutine runs in one context: Tortoise keeps its state in a
    # context variable, which a task's copied context would drop.
    shared = contextvars.copy_context()

    def complete(coro):
        return loop.run_until_complete(loop.create_task(coro, context=shared))

    module = importlib.import_module(f"benchmarks.kitchen_sink.backend_{backend_name}")
    t0 = time.perf_counter()
    if backend_name == "tortoise":
        complete(module.init())
        if args.reseed or not complete(module.is_seeded(len(data.posts))):
            complete(module.seed(data))
            print(f"  seeded in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
    elif args.reseed or not module.is_seeded(len(data.posts)):
        module.seed(data)
        print(f"  seeded in {time.perf_counter() - t0:.1f}s", file=sys.stderr)

    current.backend = module
    t0 = time.perf_counter()
    from benchmarks.kitchen_sink.schema import schema

    build_ms = (time.perf_counter() - t0) * 1000
    module.install_recorder()
    kind, run = module.make_runner(schema, mode)

    workloads = [
        w for w in WORKLOADS if not args.only or w.name in args.only.split(",")
    ]
    prof_dir = pathlib.Path(module.DB_DIR) / "prof"
    prof_dir.mkdir(exist_ok=True)
    out = {
        "variant": args.variant,
        "schema_build_ms": round(build_ms, 1),
        "rows": data.counts(),
        "workloads": {},
    }

    for workload in workloads:

        def once(workload=workload):
            return run(workload)

        try:
            if args.show_sql == workload.name:
                harness.RECORDER.reset(keep_statements=True)
                if kind == "sync":
                    once()
                else:
                    complete(once())
                for i, sql in enumerate(harness.RECORDER.statements, 1):
                    print(f"--- [{i}] ---\n{sql}\n")
                dump = prof_dir.parent / "sql" / f"{args.variant}.{workload.name}.json"
                dump.parent.mkdir(exist_ok=True)
                dump.write_text(
                    json.dumps(
                        {
                            "statements": harness.RECORDER.statements,
                            "params": [list(p) for p in harness.RECORDER.params],
                        },
                        default=str,
                    )
                )
            bench_args = {"budget_ms": args.budget_ms, "min_runs": args.min_runs}
            if kind == "sync":
                sample, result = harness.bench_sync(once, **bench_args)
            else:
                sample, result = complete(harness.bench_async(once, **bench_args))
        except Exception as exc:  # noqa: BLE001
            out["workloads"][workload.name] = {"error": repr(exc)[:500]}
            print(f"  {workload.name:<26} ERROR {repr(exc)[:300]}", file=sys.stderr)
            continue
        row = {
            **sample.summary(),
            "fingerprint": harness.fingerprint(result),
            **_shape(result),
        }
        if args.profile:
            path = str(prof_dir / f"{args.variant}.{workload.name}.prof")
            if kind == "sync":
                stats = harness.profile_sync(once, args.profile_runs, path)
            else:
                stats = complete(harness.profile_async(once, args.profile_runs, path))
            row["layers"] = harness.profile_layers(stats)
            row["orm_cumulative"] = harness.top_functions(
                stats, "/strawberry_orm/", 3, 20
            )
            row["hot_tottime"] = harness.top_functions(stats, "", 2, 20)
            row["profile_path"] = path
        out["workloads"][workload.name] = row
        print(
            f"  {workload.name:<26} {row['median_ms']:9.1f} ms  "
            f"(sql {row['sql_ms']:7.1f}, python {row['python_ms']:7.1f})  "
            f"{row['statements']:4d} stmts  {row['objects']:6d} objs",
            file=sys.stderr,
        )
        if args.profile:
            layers = ", ".join(f"{k} {v}%" for k, v in list(row["layers"].items())[:6])
            print(f"      layers: {layers}", file=sys.stderr)
    if hasattr(module, "close"):
        complete(module.close())
    loop.close()
    return out


def print_comparison(results: list[dict]) -> None:
    names = []
    for result in results:
        for name in result["workloads"]:
            if name not in names:
                names.append(name)
    variants = [r["variant"] for r in results]
    width = 22
    print("\nMedian wall time, ms (statements)")
    print(f"{'workload':<26}" + "".join(f"{v:>{width}}" for v in variants))
    for name in names:
        cells = []
        for result in results:
            row = result["workloads"].get(name)
            if not row:
                cells.append("-")
            elif "error" in row:
                cells.append("ERROR")
            else:
                cells.append(f"{row['median_ms']:.1f} ({row['statements']})")
        print(f"{name:<26}" + "".join(f"{c:>{width}}" for c in cells))
    print("\nPython share of wall time (wall - time inside the driver), ms")
    print(f"{'workload':<26}" + "".join(f"{v:>{width}}" for v in variants))
    for name in names:
        cells = []
        for result in results:
            row = result["workloads"].get(name)
            if not row or "error" in row:
                cells.append("-")
            else:
                cells.append(f"{row['python_ms']:.1f} / sql {row['sql_ms']:.1f}")
        print(f"{name:<26}" + "".join(f"{c:>{width}}" for c in cells))
    print("\nResult agreement (objects returned, totalCount)")
    for name in names:
        shapes = {
            r["variant"]: (
                r["workloads"][name].get("objects"),
                r["workloads"][name].get("total_count"),
            )
            for r in results
            if name in r["workloads"] and "error" not in r["workloads"][name]
        }
        agree = len(set(shapes.values())) <= 1
        print(
            f"  {name:<26} {'agree' if agree else 'DIFFER'} {shapes if not agree else next(iter(shapes.values()), '')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default="all", choices=["all", *VARIANTS])
    parser.add_argument("--only", default="", help="comma-separated workload names")
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--profile-runs", type=int, default=5)
    parser.add_argument("--budget-ms", type=float, default=3000)
    parser.add_argument("--min-runs", type=int, default=5)
    parser.add_argument("--show-sql", default="", help="print one run's statements")
    parser.add_argument("--json", default="", help="write results here")
    args = parser.parse_args()

    if args.variant != "all":
        print(f"== {args.variant}", file=sys.stderr)
        result = run_variant(args)
        if args.json:
            pathlib.Path(args.json).write_text(
                json.dumps(result, indent=2, default=str)
            )
        else:
            print_comparison([result])
        sys.stdout.flush()
        sys.stderr.flush()
        # A driver's worker thread must not keep the process alive.
        os._exit(0)

    results = []
    for variant in VARIANTS:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as handle:
            path = handle.name
        argv = [
            sys.executable,
            "-m",
            "benchmarks.kitchen_sink.run",
            "--variant",
            variant,
            "--json",
            path,
        ]
        for flag in ("only", "show_sql"):
            if getattr(args, flag):
                argv += [f"--{flag.replace('_', '-')}", getattr(args, flag)]
        argv += [
            "--scale",
            str(args.scale),
            "--budget-ms",
            str(args.budget_ms),
            "--min-runs",
            str(args.min_runs),
            "--profile-runs",
            str(args.profile_runs),
        ]
        if args.reseed:
            argv.append("--reseed")
        if args.profile:
            argv.append("--profile")
        completed = subprocess.run(argv, env=os.environ.copy())
        if completed.returncode == 0:
            results.append(json.loads(pathlib.Path(path).read_text()))
    print_comparison(results)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
