"""Стенд масштабу — гейт пам'яті й часу для
core.scan. «Доведено ~300k» мусить лишатись доказовим твердженням, не
переказом: цей файл ловить OOM і квадратичні регресії РАНО, до того як
власник побачить їх на реальному диску.

Два підтести:
  * легкий (1000 файлів) — БЕЗ skipif, у звичайному гейті. Перевіряє САМЕ
    те саме твердження (склад груп після core.scan збігається з очікуваним
    із генератора), тільки на малому N — щоб генератор і твердження не
    гнили непоміченими між важкими прогонами.
  * важкий (100k + 300k файлів) — під DUPSCAN_SCALE=1, міряє піковий RSS і
    час у ІЗОЛЬОВАНИХ підпроцесах (як scripts/perf_gate.py): ru_maxrss —
    пік ЗА ввесь процес, не за виклик, тож 100k і 300k в одному процесі
    отруїли б один одного накопиченим піком (див. qa/mem_benchmark.py,
    там та сама причина розписана в докстрингу режимів).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

import dupscan.domain.core as core  # noqa: E402
from qa.scale_tree import build_scale_tree  # noqa: E402

PY = os.path.join(ROOT, ".venv", "bin", "python")

# Стеля RSS/файл (байти, macOS ru_maxrss — див. коментар нижче й
# scripts/scale_bench.py). Виміряно на цій машині (16 ГіБ), dup_ratio=0.3,
# fanout=64: 100k -> 2932 Б/файл (delta=279.6 МіБ, 6.78s), 300k -> 2926
# Б/файл (delta=837.1 МіБ, 24.58s) — ru_maxrss узятий ПІСЛЯ побудови
# дерева (перед core.scan), тож delta = внесок САМЕ core.scan/_aggregate.
# Стеля — 6000 Б/файл, запас ×~2 від найгіршого виміряного (2932), щоб шум
# носія й malloc-фрагментація між прогонами не зривали гейт на регресії,
# яка ще не сталась.
MAX_RSS_PER_FILE = 6000

# час(300k)/час(100k) < цей поріг ловить квадратичну регресію: лінійний
# алгоритм на 3× файлів дає ~3×, з запасом до 4× під шум/іншу структуру ФС.
MAX_TIME_RATIO = 4.0

# subprocess-раннер: будує дерево і сканує його в ІЗОЛЬОВАНОМУ процесі,
# щоб ru_maxrss (пік за ввесь процес, не за виклик) не змішував заміри
# 100k і 300k між собою. baseline знімається ПІСЛЯ побудови дерева (перед
# core.scan), тож delta = внесок САМЕ core.scan/_aggregate у пік пам'яті.
_RUNNER = r"""
import gc, json, os, resource, sys, time
sys.path.insert(0, sys.argv[1])
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
    "group_paths": sum(len(g.paths) for g in dup_groups),
    "files_seen": res.files_seen,
    "errors": len(res.errors),
    "elapsed": elapsed,
    "rss_baseline": baseline,
    "rss_peak": peak,
    "rss_delta": max(0, peak - baseline),
}))
"""


def _manifest(root: str) -> list[tuple[str, bytes]]:
    """Відносний шлях + вміст кожного файла під root, відсортовано —
    для звірки детермінованості генератора між двома прогонами."""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            with open(fp, "rb") as fh:
                data = fh.read()
            out.append((os.path.relpath(fp, root), data))
    return sorted(out)


def _measure_subprocess(n_files: int, dup_ratio: float, fanout: int) -> dict:
    runner_dir = tempfile.mkdtemp(prefix="scale_runner_")
    runner_path = os.path.join(runner_dir, "_r.py")
    with open(runner_path, "w") as fh:
        fh.write(_RUNNER)
    tree_dir = tempfile.mkdtemp(prefix="scale_tree_")
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


def test_scale_tree_generator_is_deterministic(tmp_path):
    """Той самий (n_files, dup_ratio, fanout) -> побайтово те саме дерево
    у двох незалежних прогонах (жодного random/Date.now у генераторі)."""
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    stats_a = build_scale_tree(a, 1000, 0.3, 8)
    stats_b = build_scale_tree(b, 1000, 0.3, 8)
    assert stats_a == stats_b
    assert _manifest(a) == _manifest(b)


def test_scan_matches_generator_composition_1000():
    """Легкий підтест (без skipif): те саме твердження про склад груп, що
    й важкий, лише на 1000 файлах — щоб перевірялось у КОЖНОМУ гейті."""
    tree_dir = tempfile.mkdtemp(prefix="scale_light_")
    try:
        stats = build_scale_tree(tree_dir, 1000, 0.3, 8)
        res = core.scan([tree_dir])
        assert not res.errors, res.errors[:5]
        assert res.files_seen == stats["n_files"]
        dup_groups = [g for g in res.file_groups if len(g.paths) >= 2]
        assert len(dup_groups) == stats["expected_groups"]
        assert sum(len(g.paths) for g in dup_groups) == stats["n_dup_pairs"] * 2
        assert all(len(g.paths) == 2 for g in dup_groups)
    finally:
        shutil.rmtree(tree_dir, ignore_errors=True)


@pytest.mark.skipif(
    os.environ.get("DUPSCAN_SCALE") != "1",
    reason="важкий: 100k+300k реальних файлів на диску, лише за DUPSCAN_SCALE=1",
)
def test_scan_scales_within_memory_and_time():
    sizes = [100_000, 300_000]
    measured: dict[int, dict] = {}
    for n in sizes:
        measured[n] = _measure_subprocess(n, dup_ratio=0.3, fanout=64)

    for n, m in measured.items():
        assert m["errors"] == 0, f"{n}: несподівані помилки скану: {m['errors']}"
        assert m["files_seen"] == m["n_files"] == n, (
            f"{n}: files_seen={m['files_seen']} n_files={m['n_files']}"
        )
        assert m["groups"] == m["expected_groups"], (
            f"{n}: груп {m['groups']}, очікувалось {m['expected_groups']} — "
            f"склад ламається на масштабі"
        )
        bytes_per_file = m["rss_delta"] / m["n_files"]
        assert bytes_per_file < MAX_RSS_PER_FILE, (
            f"{n}: {bytes_per_file:.0f} Б/файл (delta={m['rss_delta']}) "
            f"понад стелю {MAX_RSS_PER_FILE} Б/файл"
        )

    ratio = measured[300_000]["elapsed"] / measured[100_000]["elapsed"]
    assert ratio < MAX_TIME_RATIO, (
        f"час 300k/час 100k = {ratio:.2f} (100k={measured[100_000]['elapsed']:.2f}s, "
        f"300k={measured[300_000]['elapsed']:.2f}s) — квадратична регресія?"
    )
