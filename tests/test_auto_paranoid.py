"""Автопараноя за файловою системою.

metadata-shortcut перед Кошиком: доказ full + незмінні метадані
-> без перечитування (DUPSCAN_PARANOID=1 і далі повертає повне читання
вручну). На exFAT mtime має крок 10 мс, а ctime несправжній — підміна
вмісту з тим самим розміром у цьому вікні непомітна для identity, тобто
сам shortcut там слабший. Диск власника — exFAT.

Автопараноя: shortcut дозволений лише коли ФС жертви у довіреному
whitelist ({"apfs", "hfs"}) — визначається через ctypes statfs ->
f_fstypename, БЕЗ читання файла. DUPSCAN_PARANOID=1 лишається сильнішим
за все (незалежно від ФС).
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
from qa.fs_images import hdiutil_available, mount_image, unmount  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


class ReadMeter:
    """Лічильник байтів, реально прочитаних із файлів через core._hash_file."""

    def __init__(self, monkeypatch):
        self.bytes = 0
        self.files: list[str] = []
        real = core._hash_file

        def spy(path, size, full, cancel, pause=None, *a, **k):
            self.bytes += size if full else min(core.PARTIAL, size)
            self.files.append(path)
            return real(path, size, full, cancel, pause, *a, **k)

        monkeypatch.setattr(core, "_hash_file", spy)


# ---- (а) юніт helper-а: whitelist за fstypename ----------------------------


def test_helper_true_for_apfs_and_hfs(tmp_path, monkeypatch):
    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: "apfs")
    assert fsops._fs_trusts_metadata(str(tmp_path)) is True

    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: "hfs")
    assert fsops._fs_trusts_metadata(str(tmp_path)) is True


def test_helper_false_for_exfat_unknown_and_statfs_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: "exfat")
    assert fsops._fs_trusts_metadata(str(tmp_path)) is False

    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: "msdos")
    assert fsops._fs_trusts_metadata(str(tmp_path)) is False

    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: "smbfs")
    assert fsops._fs_trusts_metadata(str(tmp_path)) is False

    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: "totally-unknown-fs")
    assert fsops._fs_trusts_metadata(str(tmp_path)) is False

    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    monkeypatch.setattr(fsops, "_statfs_fstype", lambda p: None)  # помилка statfs
    assert fsops._fs_trusts_metadata(str(tmp_path)) is False


def test_helper_caches_by_st_dev_not_repeating_statfs(tmp_path, monkeypatch):
    monkeypatch.setattr(fsops, "_fs_trust_cache", {})
    calls = []

    def spy(p):
        calls.append(p)
        return "apfs"

    monkeypatch.setattr(fsops, "_statfs_fstype", spy)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert fsops._fs_trusts_metadata(str(a)) is True
    assert fsops._fs_trusts_metadata(str(b)) is True  # той самий st_dev
    assert len(calls) == 1, f"statfs мав викликатись раз на st_dev, а не {len(calls)}"


# ---- (б) на РЕАЛЬНОМУ exFAT: перед Кошиком завжди повне перечитування -----


@pytest.mark.skipif(
    os.environ.get("DUPSCAN_FS_MATRIX") != "1" or not hdiutil_available(),
    reason="потрібен hdiutil: увімкнути DUPSCAN_FS_MATRIX=1",
)
def test_exfat_metadata_shortcut_forced_to_full_read(monkeypatch):
    import dupscan.ui.app as app_mod

    _image, mount_point = mount_image("ExFAT", size_mb=32)
    try:
        root = Path(mount_point) / "t"
        data = os.urandom(64 * 1024)
        make(root / "A" / "f.bin", data)
        make(root / "B" / "f.bin", data)
        r = core.scan([str(root)])
        assert r.file_groups, "має утворитися група дублікатів на exFAT-образі"
        victim = r.file_groups[0].paths[0]

        meter = ReadMeter(monkeypatch)
        monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: [])
        errors = app_mod._verified_to_trash_files(r, [victim])

        assert errors == [], f"доказ дійсний, видалення не мало відмовити: {errors}"
        assert meter.bytes > 0, (
            "exFAT: доказ full + незмінені метадані все одно мають бути "
            "перечитані перед Кошиком (D3, автопараноя за ФС)")
    finally:
        unmount(mount_point)


# ---- (в) на APFS (tmp_path): metadata-shortcut без повного читання -------


def test_apfs_metadata_shortcut_still_reads_zero_bytes(tmp_path, monkeypatch):
    import dupscan.ui.app as app_mod

    data = os.urandom(2 * 1024 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]

    meter = ReadMeter(monkeypatch)
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: [])
    errors = app_mod._verified_to_trash_files(r, [victim])

    assert errors == []
    assert meter.bytes == 0, (
        f"APFS: чинний повний доказ -> нуль читань (2.16), а прочитано "
        f"{meter.bytes} Б — регресія швидкості")


def test_paranoid_env_overrides_trusted_fs(tmp_path, monkeypatch):
    """DUPSCAN_PARANOID=1 лишається сильнішим за все, і на довіреній ФС теж."""
    import dupscan.ui.app as app_mod

    data = os.urandom(180 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    monkeypatch.setattr(core, "PARANOID_VERIFICATION", True, raising=False)

    meter = ReadMeter(monkeypatch)
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: [])
    app_mod._verified_to_trash_files(r, [victim])

    assert meter.bytes > 0, "DUPSCAN_PARANOID мусить примусити читання навіть на APFS"
