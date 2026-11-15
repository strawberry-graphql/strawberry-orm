"""Compare a results file against a baseline, workload by workload.

.venv/bin/python -m benchmarks.kitchen_sink.compare /tmp/ks_bench/baseline.json new.json
"""

from __future__ import annotations

import json
import sys


def _by_variant(path: str) -> dict[str, dict]:
    with open(path) as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        data = [data]
    return {result["variant"]: result["workloads"] for result in data}


def main() -> None:
    base, new = _by_variant(sys.argv[1]), _by_variant(sys.argv[2])
    for variant, workloads in new.items():
        before = base.get(variant, {})
        print(f"== {variant}")
        print(
            f"  {'workload':<26}{'before':>10}{'after':>10}{'speedup':>9}"
            f"{'stmts':>12}  result"
        )
        for name, row in workloads.items():
            old = before.get(name)
            if not old or "error" in old or "error" in row:
                print(f"  {name:<26} {row.get('error', 'no baseline')[:60]}")
                continue
            same = "same" if old["fingerprint"] == row["fingerprint"] else "DIFFERENT"
            print(
                f"  {name:<26}{old['median_ms']:>10.1f}{row['median_ms']:>10.1f}"
                f"{old['median_ms'] / row['median_ms']:>8.2f}x"
                f"{old['statements']:>6} ->{row['statements']:>4}  {same}"
            )


if __name__ == "__main__":
    main()
