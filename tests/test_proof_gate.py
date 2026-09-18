"""F1: доказ замість повторного читання.

Група існує лише з повних BLAKE3 зі скану. Отже перед Кошиком достатньо
довести, що файл не змінився. Тест міряє РЕАЛЬНІ прочитані байти.
"""

import os
import sys
import tempfile
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


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

    def reset(self):
        self.bytes = 0
        self.files.clear()


# ---- контракт доказу -------------------------------------------------------


def test_proof_helper_is_single_source(tmp_path):
    """proof_is_current — ЄДИНИЙ хелпер порівняння метаданих."""
    p = tmp_path / "a.bin"
    make(p, b"A" * 5000)
    st = os.lstat(p)
    fi = core.FileInfo(
        str(p), st.st_size, st.st_mtime_ns, st.st_mtime_ns, st.st_ctime_ns, st.st_dev, st.st_ino
    )
    assert core.proof_is_current(fi, st) is True
    time.sleep(0.01)
    p.write_bytes(b"B" * 5000)  # той самий розмір, інший mtime
    assert core.proof_is_current(fi, os.lstat(p)) is False


def test_class_proof_kind_is_full_for_group_members(tmp_path):
    """Кожен член групи мусить мати доказ типу full — інакше дія заборонена."""
    data = os.urandom(300 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    for g in r.file_groups:
        for path in g.paths:
            assert core.proof_kind(r, path) == "full", f"{path}: член групи без повного доказу"


def test_small_file_group_also_has_full_proof(tmp_path):
    """Однопрохідна воронка: файл ≤PARTIAL теж має ПОВНИЙ доказ."""
    data = b"S" * 4096
    make(tmp_path / "t/A/s.bin", data)
    make(tmp_path / "t/B/s.bin", data)
    r = core.scan([str(tmp_path / "t")])
    assert r.file_groups
    for path in r.file_groups[0].paths:
        assert core.proof_kind(r, path) == "full"


# ---- нуль читань на чинному доказі -----------------------------------------


def test_trash_with_valid_proof_reads_zero_bytes(tmp_path, monkeypatch):
    import dupscan.ui.app as app_mod

    data = os.urandom(2 * 1024 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    meter = ReadMeter(monkeypatch)
    trashed: list[str] = []
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    victim = r.file_groups[0].paths[0]
    errors = app_mod._verified_to_trash_files(r, [victim])
    assert errors == [], f"видалення не мусило впасти: {errors}"
    assert trashed == [victim]
    assert meter.bytes == 0, (
        f"чинний повний доказ -> нуль читань, а прочитано {meter.bytes} Б ({meter.files})"
    )


def test_changed_mtime_forces_rehash_and_refuses_on_different_content(tmp_path, monkeypatch):
    import dupscan.ui.app as app_mod

    data = os.urandom(200 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    time.sleep(0.01)
    with open(victim, "wb") as fh:  # той самий розмір, ІНШИЙ вміст
        fh.write(os.urandom(200 * 1024))
    meter = ReadMeter(monkeypatch)
    trashed: list[str] = []
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    errors = app_mod._verified_to_trash_files(r, [victim])
    assert trashed == [], "змінений файл НЕ можна видаляти"
    assert errors, "мусить бути пояснення відмови"
    assert meter.bytes > 0, "змінені метадані мусять змусити перехешування"


def test_untouched_file_after_metadata_touch_is_rehashed_and_allowed(tmp_path, monkeypatch):
    """Дотик до mtime без зміни вмісту: перехешування підтверджує digest."""
    import dupscan.ui.app as app_mod

    data = os.urandom(150 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    st = os.lstat(victim)
    os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    meter = ReadMeter(monkeypatch)
    trashed: list[str] = []
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    errors = app_mod._verified_to_trash_files(r, [victim])
    assert errors == [], f"вміст той самий -> дозволено: {errors}"
    assert trashed == [victim]
    assert meter.bytes > 0, "після зміни mtime читання обов'язкове"


def test_survivor_verified_once_per_class(tmp_path, monkeypatch):
    """Незалежна копія перевіряється один раз на клас, не на кожну жертву."""
    import dupscan.ui.app as app_mod

    data = os.urandom(120 * 1024)
    for top in ("A", "B", "C", "D"):
        make(tmp_path / "t" / top / "f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    group = r.file_groups[0]
    victims = group.paths[:3]
    survivor = group.paths[3]
    # змусити читання: доторкнутися до mtime усіх жертв
    for v in victims:
        st = os.lstat(v)
        os.utime(v, ns=(st.st_atime_ns, st.st_mtime_ns + 20_000_000))
    meter = ReadMeter(monkeypatch)
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: [])
    app_mod._verified_to_trash_files(r, victims)
    assert meter.files.count(survivor) <= 1, (
        f"копія перечитана {meter.files.count(survivor)} разів замість ≤1"
    )


def test_paranoid_mode_reads_everything(tmp_path, monkeypatch):
    import dupscan.ui.app as app_mod
    import dupscan.infra.preferences as preferences

    data = os.urandom(180 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    monkeypatch.setattr(preferences, "paranoid_verification", lambda: True, raising=False)
    monkeypatch.setattr(core, "PARANOID_VERIFICATION", True, raising=False)
    meter = ReadMeter(monkeypatch)
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: [])
    app_mod._verified_to_trash_files(r, [victim])
    assert meter.bytes > 0, "параноїдальний режим мусить читати"


def test_no_proof_means_refusal(tmp_path, monkeypatch):
    """Файл без доказу НІКОЛИ не видаляється."""
    import dupscan.ui.app as app_mod

    make(tmp_path / "t/A/f.bin", os.urandom(90 * 1024))
    r = core.scan([str(tmp_path / "t")])
    lonely = str(tmp_path / "t/A/f.bin")
    trashed: list[str] = []
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    errors = app_mod._verified_to_trash_files(r, [lonely])
    assert trashed == [] and errors, "унікальний файл не має доказу дубліката"


@pytest.mark.parametrize("kind", ["partial", "ends", "sample"])
def test_non_full_proof_is_upgraded_before_action(tmp_path, monkeypatch, kind):
    """Доказ не типу full підвищується повним читанням перед дією."""
    import dupscan.ui.app as app_mod

    data = os.urandom(400 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = r.file_groups[0].paths[0]
    r.proof_kind_override = {victim: kind}
    meter = ReadMeter(monkeypatch)
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: [])
    app_mod._verified_to_trash_files(r, [victim])
    assert meter.bytes > 0, f"доказ {kind} мусить бути підвищений читанням"
