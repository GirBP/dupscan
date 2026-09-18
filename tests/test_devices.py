"""Тип носія й removable-детекція.

Адаптовано з DupFinder tests/test_devices.py — той самий kill-switch-
патерн джерела: підмінити _run (обгортку над diskutil), скинути
lru_cache між тестами так само, як робить оригінал.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.infra.devices as devices  # noqa: E402


def _reset_caches() -> None:
    devices._media_type_cached.cache_clear()
    devices._is_removable_cached.cache_clear()


def test_media_type_ssd_from_diskutil(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(devices, "_run", lambda cmd: "   Solid State:              Yes\n")
    assert devices.media_type("/tmp") == devices.MEDIA_SSD
    _reset_caches()


def test_media_type_hdd_from_diskutil(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(devices, "_run", lambda cmd: "   Solid State:              No\n")
    assert devices.media_type("/tmp") == devices.MEDIA_HDD
    _reset_caches()


def test_media_type_unknown_when_field_absent(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(devices, "_run", lambda cmd: "")
    assert devices.media_type("/tmp") == devices.MEDIA_UNKNOWN
    _reset_caches()


def test_media_type_probes_diskutil_once_per_volume(monkeypatch):
    """Кешується per-том (lru_cache) — другий виклик тим самим шляхом не
    повторює diskutil-підпроцес."""
    _reset_caches()
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return "Solid State: Yes\n"

    monkeypatch.setattr(devices, "_run", fake_run)
    devices.media_type("/tmp")
    devices.media_type("/tmp")
    assert len(calls) == 1
    _reset_caches()


def test_media_type_never_raises_on_command_failure(monkeypatch):
    _reset_caches()

    def boom(cmd):
        raise OSError("diskutil відсутній")

    monkeypatch.setattr(devices, "_run", boom)
    assert devices.media_type("/tmp") == devices.MEDIA_UNKNOWN
    _reset_caches()


def test_is_removable_detects_external_device_location(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(devices, "_run", lambda cmd: "Device Location: External\n")
    assert devices.is_removable_path("/Volumes/Fake") is True
    _reset_caches()


def test_is_removable_false_for_internal_disk(monkeypatch):
    _reset_caches()
    monkeypatch.setattr(
        devices, "_run",
        lambda cmd: "Device Location: Internal\nRemovable Media: Fixed\nInternal: Yes\n")
    assert devices.is_removable_path("/") is False
    _reset_caches()


def test_is_removable_never_raises(monkeypatch):
    _reset_caches()

    def boom(cmd):
        raise OSError("diskutil відсутній")

    monkeypatch.setattr(devices, "_run", boom)
    assert devices.is_removable_path("/Volumes/Fake") is False
    _reset_caches()


def test_adaptive_cap_by_media(monkeypatch):
    monkeypatch.setattr(devices, "media_type", lambda p: devices.MEDIA_HDD)
    assert devices.adaptive_cap(["/a"]) == 4
    monkeypatch.setattr(devices, "media_type", lambda p: devices.MEDIA_UNKNOWN)
    assert devices.adaptive_cap(["/a"]) == 8
    monkeypatch.setattr(devices, "media_type", lambda p: devices.MEDIA_SSD)
    assert devices.adaptive_cap(["/a"]) == 16


def test_adaptive_cap_worst_media_wins_across_roots(monkeypatch):
    """Один HDD-корінь серед SSD-коренів досі каже "будь обережний"."""
    kinds = {"/ssd": devices.MEDIA_SSD, "/hdd": devices.MEDIA_HDD}
    monkeypatch.setattr(devices, "media_type", lambda p: kinds[p])
    assert devices.adaptive_cap(["/ssd", "/hdd"]) == 4
