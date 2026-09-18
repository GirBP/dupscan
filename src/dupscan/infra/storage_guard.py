"""Conservative free-space preflight for DupScan's private data store."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import shutil
from typing import Callable, NamedTuple

import dupscan.infra.preferences as preferences

_MIB = 1024 * 1024
_GIB = 1024 * _MIB

_CRITICAL_ABSOLUTE = 512 * _MIB
_CRITICAL_RATIO = 0.002
_CRITICAL_RATIO_CAP = 2 * _GIB
_LOW_ABSOLUTE = 5 * _GIB
_LOW_RATIO = 0.02
_LOW_RATIO_CAP = 10 * _GIB


class StorageLevel(str, Enum):
    SUFFICIENT = "sufficient"
    LOW = "low"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


@dataclass(frozen=True)
class StorageStatus:
    level: StorageLevel
    data_path: str
    probe_path: str
    free_bytes: int | None
    total_bytes: int | None
    required_bytes: int | None
    error: str = ""


def thresholds(total_bytes: int) -> tuple[int, int]:
    """Return ``(critical, low)`` reserves for a filesystem of *total_bytes*."""
    total = max(0, int(total_bytes))
    critical = max(
        _CRITICAL_ABSOLUTE,
        min(_CRITICAL_RATIO_CAP, int(total * _CRITICAL_RATIO)),
    )
    low = max(
        _LOW_ABSOLUTE,
        min(_LOW_RATIO_CAP, int(total * _LOW_RATIO)),
    )
    return critical, max(critical + 1, low)


def _nearest_existing_path(
    path: str,
    *,
    exists: Callable[[str], bool],
) -> str:
    candidate = os.path.abspath(os.path.expanduser(path))
    while not exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            raise OSError(f"немає доступного батьківського тому для {path}")
        candidate = parent
    return candidate


def _require_named_volume_if_applicable(
    path: str,
    *,
    exists: Callable[[str], bool],
) -> None:
    prefix = os.path.join(os.sep, "Volumes") + os.sep
    if not path.startswith(prefix):
        return
    remainder = path[len(prefix):]
    volume_name = remainder.split(os.sep, 1)[0]
    if volume_name and not exists(os.path.join(prefix, volume_name)):
        raise OSError(f"том не змонтовано: {volume_name}")


def probe_storage(
    data_path: str | None = None,
    *,
    usage: Callable[[str], object] = shutil.disk_usage,
    exists: Callable[[str], bool] = os.path.exists,
) -> StorageStatus:
    """Inspect the volume that will contain cache and sessions.

    Failures are represented as ``UNKNOWN`` so callers never mistake a failed
    probe for a safe amount of free space.
    """
    raw_path = data_path or preferences.default_data_dir()
    requested = os.fspath(raw_path)
    try:
        requested = os.path.abspath(os.path.expanduser(requested))
        _require_named_volume_if_applicable(requested, exists=exists)
        probe_path = _nearest_existing_path(requested, exists=exists)
        raw = usage(probe_path)
        total = int(getattr(raw, "total"))
        free = int(getattr(raw, "free"))
        if total <= 0 or free < 0 or free > total:
            raise OSError("файлова система повернула некоректний обсяг")
        critical, low = thresholds(total)
        if free < critical:
            return StorageStatus(
                StorageLevel.CRITICAL,
                requested,
                probe_path,
                free,
                total,
                critical,
            )
        if free < low:
            return StorageStatus(
                StorageLevel.LOW,
                requested,
                probe_path,
                free,
                total,
                low,
            )
        return StorageStatus(
            StorageLevel.SUFFICIENT,
            requested,
            probe_path,
            free,
            total,
            low,
        )
    except (OSError, TypeError, ValueError, AttributeError) as error:
        return StorageStatus(
            StorageLevel.UNKNOWN,
            requested,
            "",
            None,
            None,
            None,
            str(error),
        )
