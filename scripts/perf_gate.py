#!/usr/bin/env python3
"""Контрольований перф-харнес DupScan.

Проти шуму носія: конфігурації чергуються (не блоками), N повторів,
звіт min/median у JSON. Еталонне дерево будується детерміновано в tmp.

Використання:
  .venv/bin/python scripts/perf_gate.py            # швидкий прогін (3 повтори)
  .venv/bin/python scripts/perf_gate.py --repeats 7
  .venv/bin/python scripts/perf_gate.py --tree /шлях/до/реального/дерева
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

RUNNER = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, os.path.join(sys.argv[1], "src"))
import dupscan.domain.core as core
t = time.monotonic()
r = core.scan([sys.argv[2]])
print(json.dumps({
    "time": round(time.monotonic() - t, 4),
    "files": r.files_seen,
    "groups": len(r.file_groups),
    "dir_groups": len(r.dir_groups),
    "pairs": len(r.sim_pairs),
}))
"""


def build_reference_tree(base: str) -> int:
    payloads = [bytes([i % 251]) * (4096 + i * 37) for i in range(200)]
    n = 0
    for d in range(900):
        p = os.path.join(base, f"dir{d}")
        os.makedirs(p, exist_ok=True)
        for f in range(30):
            data = (
                payloads[(d * 30 + f) % 200]
                if f % 3 == 0
                else bytes([(d + f) % 251]) * (2048 + (d * f) % 60000)
            )
            with open(os.path.join(p, f"f{f}.bin"), "wb") as fh:
                fh.write(data)
            n += 1
    big = os.path.join(base, "big")
    os.makedirs(big, exist_ok=True)
    for i in range(4):
        with open(os.path.join(big, f"b{i}.bin"), "wb") as fh:
            fh.write(b"Z" * (24 * 1024 * 1024))
        n += 1
    return n


def run_once(tree: str) -> dict:
    runner = os.path.join(tempfile.mkdtemp(), "_r.py")
    with open(runner, "w") as fh:
        fh.write(RUNNER)
    env = dict(os.environ, DUPSCAN_DATA_DIR=tempfile.mkdtemp())
    out = subprocess.run(
        [PY, runner, ROOT, tree], capture_output=True, text=True, env=env, check=True
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tree", default=None, help="існуюче дерево замість еталонного tmp")
    args = ap.parse_args()

    cleanup = None
    tree = args.tree
    if tree is None:
        tree = tempfile.mkdtemp(prefix="perfgate_")
        cleanup = tree
        n = build_reference_tree(tree)
        print(f"еталонне дерево: {n} файлів", file=sys.stderr)

    times, meta = [], None
    for i in range(args.repeats):
        d = run_once(tree)
        meta = meta or d
        if (d["files"], d["groups"]) != (meta["files"], meta["groups"]):
            print("НЕСТАБІЛЬНІ РЕЗУЛЬТАТИ між повторами — перф не має сенсу", file=sys.stderr)
            sys.exit(2)
        times.append(d["time"])
        print(f"  повтор {i + 1}/{args.repeats}: {d['time']:.2f}s", file=sys.stderr)

    print(
        json.dumps(
            {
                "repeats": args.repeats,
                "min": min(times),
                "median": statistics.median(times),
                "files": meta["files"],
                "files_per_sec_best": round(meta["files"] / min(times)),
                "groups": meta["groups"],
                "tree": args.tree or "reference",
            },
            ensure_ascii=False,
        )
    )

    if cleanup:
        shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    main()
