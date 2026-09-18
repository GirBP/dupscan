#!/usr/bin/env python3
"""Мікробенч ціни системного Кошика перед будь-якою оптимізацією.

Кошик на тому самому томі — це перейменування, дані не копіюються. Питання
лише в накладних витратах виклику NSFileManager на файл. Скрипт міряє це на
ВКАЗАНОМУ томі й прибирає за собою.

Використання:
  .venv/bin/python scripts/trash_bench.py --volume /Volumes/L --count 200
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--volume", default="/tmp")
    ap.add_argument("--count", type=int, default=200)
    ap.add_argument("--size", type=int, default=4096)
    ap.add_argument("--workers", type=int, default=1, help="1 = послідовно; >1 = пул потоків")
    args = ap.parse_args()

    import send2trash

    work = os.path.join(args.volume, f".dupscan-trash-bench-{os.getpid()}")
    os.makedirs(work, exist_ok=True)
    paths = []
    for i in range(args.count):
        p = os.path.join(work, f"b{i}.bin")
        with open(p, "wb") as fh:
            fh.write(os.urandom(args.size))
        paths.append(p)

    per_call: list[float] = []
    failures = 0

    def one(path: str):
        t0 = time.perf_counter()
        try:
            send2trash.send2trash(path)
        except Exception:  # noqa: BLE001 — бенч не має падати від одного файла
            return None
        return time.perf_counter() - t0

    wall0 = time.perf_counter()
    if args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for value in pool.map(one, paths):
                if value is None:
                    failures += 1
                else:
                    per_call.append(value)
    else:
        for path in paths:
            value = one(path)
            if value is None:
                failures += 1
            else:
                per_call.append(value)
    wall = time.perf_counter() - wall0

    shutil.rmtree(work, ignore_errors=True)
    if not per_call:
        print(
            json.dumps(
                {"error": "жоден виклик не вдався", "failures": failures}, ensure_ascii=False
            )
        )
        sys.exit(2)
    print(
        json.dumps(
            {
                "volume": args.volume,
                "count": len(per_call),
                "failures": failures,
                "median_ms": round(statistics.median(per_call) * 1000, 2),
                "mean_ms": round(statistics.fmean(per_call) * 1000, 2),
                "max_ms": round(max(per_call) * 1000, 2),
                "wall_s": round(wall, 2),
                "workers": args.workers,
                "files_per_sec": round(len(per_call) / wall),
            },
            ensure_ascii=False,
        )
    )
    print("УВАГА: файли лишились у Кошику тому — приберіть їх вручну.", file=sys.stderr)


if __name__ == "__main__":
    main()
