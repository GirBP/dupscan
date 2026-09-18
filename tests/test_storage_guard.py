"""Free-space policy is deterministic and independent of the real disk."""

import os

import dupscan.infra.storage_guard as storage_guard

_GIB = 1024 ** 3


def _usage(total_gib: int, free_gib: float) -> storage_guard.DiskUsage:
    total = int(total_gib * _GIB)
    free = int(free_gib * _GIB)
    return storage_guard.DiskUsage(total, total - free, free)


def test_storage_levels_use_absolute_and_bounded_ratio_reserves(tmp_path):
    path = str(tmp_path / "not-yet-created" / "DupScan")

    sufficient = storage_guard.probe_storage(
        path,
        usage=lambda _path: _usage(500, 20),
        exists=os.path.exists,
    )
    low = storage_guard.probe_storage(
        path,
        usage=lambda _path: _usage(500, 2),
        exists=os.path.exists,
    )
    critical = storage_guard.probe_storage(
        path,
        usage=lambda _path: _usage(500, 0.25),
        exists=os.path.exists,
    )

    assert sufficient.level is storage_guard.StorageLevel.SUFFICIENT
    assert low.level is storage_guard.StorageLevel.LOW
    assert low.required_bytes == 10 * _GIB
    assert critical.level is storage_guard.StorageLevel.CRITICAL
    assert critical.required_bytes == 1 * _GIB
    assert all(
        status.probe_path == str(tmp_path)
        for status in (sufficient, low, critical)
    )


def test_small_volume_thresholds_keep_practical_absolute_floor(tmp_path):
    status = storage_guard.probe_storage(
        str(tmp_path),
        usage=lambda _path: _usage(32, 1),
    )

    assert status.level is storage_guard.StorageLevel.LOW
    assert status.required_bytes == 5 * _GIB


def test_probe_failure_or_malformed_usage_is_unknown_not_safe(tmp_path):
    failed = storage_guard.probe_storage(
        str(tmp_path),
        usage=lambda _path: (_ for _ in ()).throw(OSError("offline")),
    )
    malformed = storage_guard.probe_storage(
        str(tmp_path),
        usage=lambda _path: storage_guard.DiskUsage(100, 0, 200),
    )

    assert failed.level is storage_guard.StorageLevel.UNKNOWN
    assert "offline" in failed.error
    assert malformed.level is storage_guard.StorageLevel.UNKNOWN
    assert failed.free_bytes is None and malformed.free_bytes is None


def test_missing_named_volume_is_unknown_without_probing_parent_volume():
    probed = []

    def exists(path):
        return path in ("/", "/Volumes")

    status = storage_guard.probe_storage(
        "/Volumes/Offline/DupScan",
        usage=lambda path: probed.append(path) or _usage(500, 20),
        exists=exists,
    )

    assert status.level is storage_guard.StorageLevel.UNKNOWN
    assert "том не змонтовано" in status.error
    assert probed == []


def test_thresholds_are_ordered_for_zero_and_large_volumes():
    for total in (0, 32 * _GIB, 500 * _GIB, 8_000 * _GIB):
        critical, low = storage_guard.thresholds(total)
        assert critical >= 512 * 1024 ** 2
        assert low >= 5 * _GIB
        assert critical < low <= 10 * _GIB
