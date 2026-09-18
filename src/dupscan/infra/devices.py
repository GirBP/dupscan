"""Тип носія й адаптивне дроселювання скану — визначення диска.

Адаптовано з DupFinder src/dupfinder/devices.py.
DupScan — виключно macOS-застосунок (packaging/,
scripts/build_macos.sh), тому перенесено ЛИШЕ macOS-гілку джерела;
Windows (``Get-PhysicalDisk``/PowerShell) і Linux (``/sys/block``,
``lsblk``) детектори не портовано — мертвий код на єдиній підтримуваній
платформі. ``auto_workers`` (легасі, за докстрінгом джерела вже
замінений feedback-губернатором) теж не портовано.

Best-effort і НІКОЛИ не кидає: ``diskutil`` недоступний/повільний/дає
несподіваний вивід → ``MEDIA_UNKNOWN`` / ``False``, дроселювання лишається
консервативним, скан не падає.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from functools import lru_cache

logger = logging.getLogger(__name__)

MEDIA_SSD = "ssd"
MEDIA_HDD = "hdd"
MEDIA_UNKNOWN = "unknown"

_SUBPROCESS_TIMEOUT = 10  # секунд; детекція не має зупиняти скан


def _run(cmd: list[str]) -> str:
    """Прогнати команду детекції, повернути stdout ('' при будь-якій помилці)."""
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        )
        return out.stdout or ""
    except Exception as exc:  # noqa: BLE001 — детекція ніколи не кидає
        logger.debug("Media detection command failed (%s): %s", cmd[0], exc)
        return ""


def _macos_media_type(path: str) -> str:
    out = _run(["diskutil", "info", os.path.abspath(path)])
    match = re.search(r"Solid State:\s*(\w+)", out)
    if match:
        return MEDIA_SSD if match.group(1).lower() == "yes" else MEDIA_HDD
    return MEDIA_UNKNOWN


def media_type(path: str) -> str:
    """``'ssd'``, ``'hdd'`` або ``'unknown'`` для тому, що містить *path*.

    Кешується per-том (lru_cache) — diskutil-виклик стається щонайбільше
    раз на диск, не раз на скановану теку.
    """
    return _media_type_cached(os.path.abspath(path))


@lru_cache(maxsize=64)
def _media_type_cached(path: str) -> str:
    try:
        result = _macos_media_type(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Media detection failed for %s: %s", path, exc)
        result = MEDIA_UNKNOWN
    logger.info("Storage media for %s: %s", path, result)
    return result


def _macos_is_removable(path: str) -> bool:
    low = _run(["diskutil", "info", os.path.abspath(path)]).lower()
    if not low:
        return False
    return bool(
        re.search(r"device location:\s*external", low)
        or re.search(r"removable media:\s*removable", low)
        or re.search(r"internal:\s*no", low)
        or re.search(r"protocol:\s*(usb|firewire)", low)
    )


def is_removable_path(path) -> bool:
    """Best-effort: True якщо *path* лежить на зовнішньому/знімному диску.

    Ніколи не кидає; невідомо → False.
    """
    return _is_removable_cached(os.path.abspath(os.fspath(path)))


@lru_cache(maxsize=64)
def _is_removable_cached(path: str) -> bool:
    try:
        return _macos_is_removable(path)
    except Exception as exc:  # noqa: BLE001 — детекція ніколи не кидає
        logger.debug("Removable detection failed for %s: %s", path, exc)
        return False


def adaptive_cap(roots) -> int:
    """Верхня межа для throttle.AdaptiveConcurrency за виявленим носієм.

    Губернатор стартує НИЗЬКО (2 читачі) і піднімається лише поки виміряна
    пропускна здатність справді росте, тож ця межа ніколи не задає робочу
    точку — лише обмежує пул і найгірший випадок, до якого підйом може
    дійти:

    * будь-де HDD      -> 4  (seek-трешинг настає майже одразу)
    * невідомий носій  -> 8  (середина)
    * усюди SSD        -> 16 (I/O-bound хешування нічого не виграє далі)
    """
    kinds = {media_type(os.path.abspath(os.fspath(r))) for r in roots}
    if MEDIA_HDD in kinds:
        cap = 4
    elif MEDIA_UNKNOWN in kinds:
        cap = 8
    else:
        cap = 16
    logger.info("Adaptive I/O cap: media=%s -> cap=%d", sorted(kinds), cap)
    return cap
