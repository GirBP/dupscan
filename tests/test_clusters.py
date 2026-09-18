"""Транзитивні кластери тек.

Адаптовано з DupFinder tests/test_duplicate_clusters.py — той самий
union-find/транзитивність, перевірено на core.ScanResult DupScan (замість
dir_path/dir_files/file_hash джерела — тут усе вже готове в ScanResult).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.domain.clusters as clusters  # noqa: E402
import dupscan.domain.core as core  # noqa: E402


def _result(
    file_class: dict[str, str],
    dir_files: dict[str, list[str]] | None = None,
    sizes: dict[str, int] | None = None,
) -> core.ScanResult:
    res = core.ScanResult()
    res.file_class = dict(file_class)
    class_paths: dict[str, list[str]] = {}
    for path, cls in file_class.items():
        class_paths.setdefault(cls, []).append(path)
    res.class_paths = class_paths
    if dir_files is None:
        dir_files = {}
        for path in file_class:
            dir_files.setdefault(os.path.dirname(path), []).append(path)
    res.dir_files = dir_files
    sizes = sizes or {}
    for path in file_class:
        res.file_meta[path] = core.FileInfo(path, sizes.get(path, 100), 1, 1)
    return res


def test_transitive_chain_forms_one_cluster():
    """A-B поділяють файл X, B-C поділяють ІНШИЙ файл Y -> A, B, C в
    одному кластері (транзитивність через спільну теку B)."""
    res = _result({
        "/a/x.bin": "cls-x",
        "/b/x.bin": "cls-x",
        "/b/y.bin": "cls-y",
        "/c/y.bin": "cls-y",
    })
    out = clusters.build_duplicate_clusters(res)
    assert len(out) == 1
    assert out[0].dirs == ["/a", "/b", "/c"]


def test_directory_without_shared_content_is_excluded():
    res = _result({
        "/a/x.bin": "cls-x",
        "/b/x.bin": "cls-x",
        "/solo/unique.bin": "u:0",  # унікальний файл — власний, ніким не поділюваний клас
    })
    out = clusters.build_duplicate_clusters(res)
    assert len(out) == 1
    all_dirs = {d for c in out for d in c.dirs}
    assert "/solo" not in all_dirs


def test_same_directory_copies_do_not_cross_link():
    """Дві копії В ОДНІЙ теці — не міжтечовий лінк, кластера немає."""
    res = _result({
        "/a/x.bin": "cls-x",
        "/a/x-copy.bin": "cls-x",
    })
    assert clusters.build_duplicate_clusters(res) == []


def test_class_count_counts_distinct_linking_classes():
    """Кластер A-B-C, зв'язаний ДВОМА різними класами (x, y) — class_count==2,
    навіть якщо dup_file_count вищий (кожен клас дає кілька примірників)."""
    res = _result({
        "/a/x.bin": "cls-x",
        "/b/x.bin": "cls-x",
        "/b/x2.bin": "cls-x",  # ще одна копія того самого класу в /b
        "/b/y.bin": "cls-y",
        "/c/y.bin": "cls-y",
    })
    out = clusters.build_duplicate_clusters(res)
    assert len(out) == 1
    assert out[0].class_count == 2
    assert out[0].dup_file_count == 5


def test_min_files_filters_small_clusters():
    res = _result({
        "/a/x.bin": "cls-x",
        "/b/x.bin": "cls-x",
    })
    assert clusters.build_duplicate_clusters(res, min_files=1) != []
    assert clusters.build_duplicate_clusters(res, min_files=3) == []


def test_percent_for_and_per_dir_totals():
    res = _result({
        "/a/x.bin": "cls-x",
        "/b/x.bin": "cls-x",
    })
    res.dir_files["/a"].append("/a/unique.bin")
    res.file_class["/a/unique.bin"] = "u:solo"
    res.class_paths["u:solo"] = ["/a/unique.bin"]
    res.file_meta["/a/unique.bin"] = core.FileInfo("/a/unique.bin", 5, 1, 1)

    out = clusters.build_duplicate_clusters(res)
    cluster = out[0]
    assert cluster.per_dir_total["/a"] == 2  # x.bin + unique.bin
    assert cluster.per_dir_dup["/a"] == 1  # лише x.bin дубльований
    assert cluster.percent_for("/a") == 50.0
    assert cluster.percent_for("/not-a-member") == 0.0


def test_large_result_stays_roughly_linear():
    """Перф-межа: 20k файлів (2k класів по 10 копій, кожна у своїй теці) —
    лінійний час, не квадратичний."""
    import time

    file_class = {}
    for cls_idx in range(2000):
        for copy_idx in range(10):
            file_class[f"/dir{cls_idx}_{copy_idx}/f.bin"] = f"cls-{cls_idx}"
    res = _result(file_class)

    started = time.perf_counter()
    out = clusters.build_duplicate_clusters(res)
    elapsed = time.perf_counter() - started

    assert len(out) == 2000
    assert elapsed < 2.0, f"perf regression: {elapsed:.3f}s"


def test_dir_cluster_has_no_selection_state():
    """Структурний read-only: DirCluster не має чекбокс-подібного поля —
    з цього типу немає шляху до Кошика, бо нема чим позначити."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(clusters.DirCluster)}
    assert "checked" not in field_names
    assert not hasattr(clusters.DirCluster, "checked")


def test_works_on_historical_session_without_disk():
    """Історична сесія — це просто ScanResult (live=False), диска не
    треба: та сама функція, ті самі дані, кластери й далі рахуються."""
    res = _result({
        "/a/x.bin": "cls-x",
        "/b/x.bin": "cls-x",
    })
    res.live = False
    res.scanned_roots = []
    out = clusters.build_duplicate_clusters(res)
    assert len(out) == 1
