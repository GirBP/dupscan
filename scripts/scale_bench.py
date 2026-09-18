#!/usr/bin/env python3
"""Ручний бенч RSS/час на 100k/300k/500k файлів.

Не тест — для власника й майбутніх регресій поза гейтом (гейт — легкий і
важкий підтести в tests/test_scale_gate.py). Стиль scripts/perf_gate.py:
конфігурації (тут — розміри дерева) чергуються не блоками, N повторів,
min/median у JSON-вивід.

Кожен вимір — в ІЗОЛЬОВАНОМУ підпроцесі: ru_maxrss (resource.getrusage,
RUSAGE_SELF) — пік ЗА ВВЕСЬ процес, не за виклик, тож два виміри в одному
процесі накопичували б пік один на одного (та сама причина, що в
qa/mem_benchmark.py — там кожен режим теж запускається окремим процесом).
baseline знімається ПІСЛЯ побудови дерева (перед core.scan), тож
delta = внесок САМЕ core.scan/_aggregate.

Використання:
  .venv/bin/python scripts/scale_bench.py                     # 100k/300k/500k, 1 повтор
  .venv/bin/python scripts/scale_bench.py --sizes 50000,100000 --repeats 3
  .venv/bin/python scripts/scale_bench.py --dup-ratio 0.5 --fanout 32
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, ".venv", "bin", "python")

_RUNNER = r"""
import gc, json, os, resource, sys, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, os.path.join(sys.argv[1], "src"))
import dupscan.domain.core as core
from qa.scale_tree import build_scale_tree

tree_dir = sys.argv[2]
n_files = int(sys.argv[3])
dup_ratio = float(sys.argv[4])
fanout = int(sys.argv[5])

stats = build_scale_tree(tree_dir, n_files, dup_ratio, fanout)
gc.collect()
baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # macOS: БАЙТИ

t0 = time.monotonic()
res = core.scan([tree_dir])
elapsed = time.monotonic() - t0

peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
dup_groups = [g for g in res.file_groups if len(g.paths) >= 2]

print(json.dumps({
    "n_files": stats["n_files"],
    "expected_groups": stats["expected_groups"],
    "groups": len(dup_groups),
    "files_seen": res.files_seen,
    "errors": len(res.errors),
    "elapsed": elapsed,
    "rss_baseline": baseline,
    "rss_peak": peak,
    "rss_delta": max(0, peak - baseline),
}))
"""


def run_once(n_files: int, dup_ratio: float, fanout: int) -> dict:
    runner_dir = tempfile.mkdtemp(prefix="scale_bench_runner_")
    runner_path = os.path.join(runner_dir, "_r.py")
    with open(runner_path, "w") as fh:
        fh.write(_RUNNER)
    tree_dir = tempfile.mkdtemp(prefix="scale_bench_tree_")
    env = dict(os.environ, DUPSCAN_DATA_DIR=tempfile.mkdtemp())
    try:
        out = subprocess.run(
            [PY, runner_path, ROOT, tree_dir, str(n_files), str(dup_ratio), str(fanout)],
            capture_output=True, text=True, env=env, check=True,
        )
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tree_dir, ignore_errors=True)
        shutil.rmtree(runner_dir, ignore_errors=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="100000,300000,500000")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--dup-ratio", type=float, default=0.3)
    ap.add_argument("--fanout", type=int, default=64)
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    # Чергування конфігурацій, не блоками — той самий принцип, що в
    # perf_gate.py: рівномірно розподіляє шум носія між розмірами, а не
    # концентрує його в одному.
    schedule = [(size, repeat) for repeat in range(args.repeats) for size in sizes]

    runs: dict[int, list[dict]] = {size: [] for size in sizes}
    for size, repeat in schedule:
        d = run_once(size, args.dup_ratio, args.fanout)
        runs[size].append(d)
        bytes_per_file = d["rss_delta"] / max(1, d["n_files"])
        print(
            f"  {size:>7,} файлів, повтор {repeat + 1}/{args.repeats}: "
            f"{d['elapsed']:.2f}s, RSS Δ={d['rss_delta'] / (1024 * 1024):.1f} МіБ "
            f"({bytes_per_file:.0f} Б/файл), груп={d['groups']}/{d['expected_groups']}",
            file=sys.stderr,
        )
        if d["groups"] != d["expected_groups"] or d["errors"]:
            print(
                f"    УВАГА: склад груп/помилки розійшлись — {d}", file=sys.stderr,
            )

    report = []
    for size in sizes:
        ds = runs[size]
        times = [d["elapsed"] for d in ds]
        bpf = [d["rss_delta"] / max(1, d["n_files"]) for d in ds]
        report.append({
            "size": size,
            "repeats": len(ds),
            "time_min": min(times),
            "time_median": statistics.median(times),
            "bytes_per_file_min": min(bpf),
            "bytes_per_file_median": statistics.median(bpf),
            "groups": ds[0]["groups"],
            "expected_groups": ds[0]["expected_groups"],
        })

    print(
        json.dumps(
            {"dup_ratio": args.dup_ratio, "fanout": args.fanout, "runs": report},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
