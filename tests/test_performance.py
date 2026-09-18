"""Stable performance gates for formerly pathological in-memory operations."""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def similarity_fixture(classes: int = 300, directories: int = 60) -> core.ScanResult:
    result = core.ScanResult()
    dirs = [f"/bench/d{i:02d}" for i in range(directories)]
    for i, directory in enumerate(dirs):
        unique = f"{directory}/unique"
        result.dir_ok[directory] = True
        result.dir_files[directory] = [unique]
        result.file_class[unique] = f"u:{i + 1}"
        result.file_meta[unique] = core.FileInfo(unique, 101 + i, 1, 1)
    for number in range(classes):
        cls = f"100:{number:064x}"
        result.class_size[cls] = 100
        paths = []
        for directory in dirs:
            path = f"{directory}/f{number:04d}"
            paths.append(path)
            result.dir_files[directory].append(path)
            result.file_class[path] = cls
            result.file_meta[path] = core.FileInfo(path, 100, 1, 1)
        result.class_paths[cls] = paths
    return result


def test_similarity_aggregation_18k_paths_stays_bounded():
    result = similarity_fixture()
    started = time.perf_counter()
    core._aggregate(result)
    elapsed = time.perf_counter() - started
    assert len(result.sim_pairs) == core.MAX_PAIRS
    assert elapsed < 2.0, f"aggregation regression: {elapsed:.3f}s"


def test_hash_worker_count_is_bounded():
    assert 2 <= core.WORKERS <= 8


def test_external_full_hashing_limits_parallel_reads_per_device():
    external = [
        core.FileInfo(f"/Volumes/Archive/file-{index}.bin", 100, 1, 1, dev=7)
        for index in range(20)
    ]
    internal = [
        core.FileInfo(f"/Users/test/file-{index}.bin", 100, 1, 1, dev=8)
        for index in range(20)
    ]

    assert core._hash_file_parallelism(external, full=True) == 2
    assert core._hash_file_parallelism(external, full=False) == core.WORKERS
    assert core._hash_file_parallelism(internal, full=True) == core.WORKERS


def test_recompute_20k_paths_with_10k_removed_stays_linear():
    result = core.ScanResult()
    root = "/bench/root"
    result.dir_ok[root] = True
    result.dir_files[root] = []
    for index in range(20_000):
        path = f"{root}/file-{index:05d}.bin"
        class_id = f"u:{index + 1}"
        result.file_meta[path] = core.FileInfo(path, 100, 1, 1)
        result.file_class[path] = class_id
        result.dir_files[root].append(path)
    removed = {
        f"{root}/file-{index:05d}.bin" for index in range(0, 20_000, 2)
    }
    started = time.perf_counter()

    assert core.recompute(result, removed)

    elapsed = time.perf_counter() - started
    assert len(result.file_meta) == 10_000
    assert elapsed < 2.0, f"recompute regression: {elapsed:.3f}s"


def test_cancelled_recompute_does_not_publish_partial_state():
    result = similarity_fixture(classes=10, directories=4)
    core._aggregate(result)
    before = (
        dict(result.file_meta),
        dict(result.file_class),
        list(result.sim_pairs),
    )
    cancel = threading.Event()
    cancel.set()

    assert not core.recompute(
        result, {next(iter(result.file_meta))}, cancel=cancel)

    assert result.file_meta == before[0]
    assert result.file_class == before[1]
    assert result.sim_pairs == before[2]
