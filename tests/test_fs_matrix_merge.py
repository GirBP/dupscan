"""Матриця ФС × ланцюг злиття: E2E-двійник власника на РЕАЛЬНОМУ exFAT-образі.

Навіщо окремо від test_owner_scenario.py: той прогін живе на tmp_path, тобто
завжди APFS. Диск власника (BARRACUDA) — exFAT, і всі п'ять хотфіксів злиття
прийшли скріншотами з нього, а не з тестів. Причина одна: жоден тест ніколи не
торкався злиття на чужій ФС. Тут той самий ланцюг (`qa.merge_chain`) і те саме
дерево (`qa.make_owner_tree`) ганяються на змонтованому образі.

exFAT відрізняється від APFS трьома речами, кожна з яких уже колись ламала
продукт або інструмент:
- немає жорстких посилань (`os.link` → ENOTSUP 45) — публікація переносу
  мусить іти фолбеком `_publish_no_overwrite` (хотфікс 4);
- регістронечутливі імена;
- права доступу емулюються монтуванням, не зберігаються.

Ізоляція ПЕРЕД імпортом Qt: DUPSCAN_DATA_DIR + offscreen, інакше тест
торкнеться реальної сесії користувача. HOME підмінюється лише поштучно
(monkeypatch), не на рівні модуля — інакше просочиться у весь прогін.
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
from qa.fs_images import hdiutil_available, mount_image, unmount  # noqa: E402
from qa.make_owner_tree import build_owner_tree  # noqa: E402
from qa.merge_chain import run_merge_prep, run_pair_verification, run_scan  # noqa: E402

_qapp = QApplication.instance() or QApplication([])

pytestmark = pytest.mark.skipif(
    os.environ.get("DUPSCAN_FS_MATRIX") != "1" or not hdiutil_available(),
    reason="потрібен hdiutil: увімкнути DUPSCAN_FS_MATRIX=1",
)


@pytest.fixture
def exfat_tree(monkeypatch, tmp_path):
    """Змонтований exFAT-том із деревом-двійником власника на ньому."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _image, mount_point = mount_image("ExFAT", size_mb=128)
    try:
        base = Path(mount_point) / "owner"
        paths = build_owner_tree(base)
        # Диск власника саме такий: жорстких посилань там бути не може.
        assert paths["hardlinks_supported"] is False, (
            "exFAT не має підтримувати жорсткі посилання — "
            "перевір, що образ справді exFAT")
        yield paths, mount_point
    finally:
        unmount(mount_point)


def test_exfat_merge_move_chain(exfat_tree):
    """Повний ланцюг переносу на exFAT: скан → пара → план → move → Кошик."""
    paths, _mount_point = exfat_tree
    downloads = paths["downloads"]
    copy = paths["copy"]

    run_scan(paths["base"])

    fresh = run_pair_verification(str(copy), str(downloads))
    assert not fresh.partial
    assert core.directory_tree_mergeable(fresh, str(copy))
    assert core.directory_tree_mergeable(fresh, str(downloads))

    plan, _total, expected_digests, _same_device, root_identities = (
        run_merge_prep(fresh, str(copy), str(downloads)))
    assert plan, "план переносу на exFAT не мав бути порожнім"

    # Вміст захоплюється ДО переносу — джерело зникне.
    collision_copy_bytes = paths["collision_copy_path"].read_bytes()
    collision_downloads_bytes = paths["collision_downloads_path"].read_bytes()
    pycache_bytes = paths["pycache_path"].read_bytes()
    symlink_target = os.readlink(paths["symlink_path"])

    completed, errors = app_mod._move_files(
        fresh, plan, str(downloads), str(copy),
        expected_digests, root_identities)

    assert errors == [], f"перенос на exFAT не мав давати помилок: {errors}"
    assert completed

    # Неспільний вміст доїхав побайтово.
    assert (downloads / paths["pycache_rel"]).read_bytes() == pycache_bytes
    assert (downloads / paths["git_rel"]).exists()
    assert (downloads / paths["cyrillic_copy_rel"]).exists()
    assert (downloads / paths["unique_copy_rel"]).exists()
    # Те, що ніколи не було в джерелі, лишилось на місці.
    assert (downloads / paths["cyrillic_downloads_rel"]).exists()
    assert (downloads / paths["unique_downloads_rel"]).exists()

    symlink_dst = downloads / paths["symlink_rel"]
    assert symlink_dst.is_symlink()
    assert os.readlink(symlink_dst) == symlink_target

    zero_dst = downloads / paths["zero_byte_rel"]
    assert zero_dst.exists() and zero_dst.stat().st_size == 0

    # Колізія імені: ціль недоторканна, джерело отримує суфікс.
    base_name, extension = os.path.splitext(paths["collision_rel"])
    original_dst = downloads / paths["collision_rel"]
    suffixed_dst = downloads / f"{base_name} (2){extension}"
    assert original_dst.read_bytes() == collision_downloads_bytes
    assert suffixed_dst.read_bytes() == collision_copy_bytes

    # Дублікати лишаються по обидва боки — саме вони доводять теку-джерело.
    for rel in paths["duplicate_rels"]:
        assert (downloads / rel).exists()
        assert (copy / rel).exists()


def test_exfat_merge_copy_leaves_source_untouched(exfat_tree):
    """Копіювання на exFAT: ціль наповнюється, джерело побайтово ціле."""
    paths, _mount_point = exfat_tree
    downloads = paths["downloads"]
    copy = paths["copy"]

    run_scan(paths["base"])
    fresh = run_pair_verification(str(copy), str(downloads))
    plan, _total, expected_digests, _same_device, root_identities = (
        run_merge_prep(fresh, str(copy), str(downloads)))
    assert plan

    before = {
        os.path.relpath(os.path.join(dirpath, name), copy):
            os.lstat(os.path.join(dirpath, name))
        for dirpath, _dirnames, filenames in os.walk(str(copy))
        for name in filenames
    }

    completed, errors = app_mod._copy_files(
        fresh, plan, str(downloads), str(copy),
        expected_digests, root_identities)

    assert errors == [], f"копіювання на exFAT не мало давати помилок: {errors}"
    assert completed

    after = {
        os.path.relpath(os.path.join(dirpath, name), copy):
            os.lstat(os.path.join(dirpath, name))
        for dirpath, _dirnames, filenames in os.walk(str(copy))
        for name in filenames
    }
    assert set(before) == set(after), "джерело не мало втратити файли"
    for rel, before_stat in before.items():
        after_stat = after[rel]
        assert before_stat.st_size == after_stat.st_size, (
            f"{rel}: джерело не мало змінитися при копіюванні")

    assert (downloads / paths["pycache_rel"]).exists()
    assert (downloads / paths["symlink_rel"]).is_symlink()
    assert (downloads / paths["zero_byte_rel"]).exists()


def test_exfat_merge_then_trash_source(exfat_tree, monkeypatch):
    """Кінець ланцюга: доказ теки-джерела і відправлення її в Кошик."""
    paths, _mount_point = exfat_tree
    downloads = paths["downloads"]
    copy = paths["copy"]

    run_scan(paths["base"])
    fresh = run_pair_verification(str(copy), str(downloads))
    plan, _total, expected_digests, _same_device, root_identities = (
        run_merge_prep(fresh, str(copy), str(downloads)))
    completed, errors = app_mod._move_files(
        fresh, plan, str(downloads), str(copy),
        expected_digests, root_identities)
    assert errors == [] and completed

    trashed: list = []

    def recorder(victims, **_kwargs):
        trashed.extend(victims)
        return []

    monkeypatch.setattr(app_mod, "to_trash", recorder)

    fresh_after = run_pair_verification(str(copy), str(downloads))
    kind, trash_errors = app_mod._verify_then_trash_dir(fresh_after, str(copy))

    assert (kind, trash_errors) == ("ok", [])
    assert trashed == [str(copy)]


def test_exfat_real_trash_uses_volume_trashes(exfat_tree):
    """Справжній Кошик на exFAT-томі: .Trashes на свіжому томі ще НЕ існує.

    Це остання ланка флоу власника, і вона єдина, яку мок ніколи не перевіряв:
    файл на зовнішньому томі не може поїхати в ~/.Trash (інший пристрій), тож
    send2trash мусить створити .Trashes/<uid> просто на томі.
    """
    paths, mount_point = exfat_tree
    victim = paths["unique_copy_path"]
    assert victim.exists()

    errors = fsops.to_trash([str(victim)])

    assert errors == [], f"Кошик на exFAT не мав давати помилок: {errors}"
    assert not victim.exists(), "файл мав зникнути з початкового шляху"
    volume_trash = os.path.join(mount_point, ".Trashes", str(os.getuid()))
    assert os.path.isdir(volume_trash), (
        "жертва мусить лежати в .Trashes тому, а не зникнути безслідно")
    assert os.listdir(volume_trash), "Кошик тому не мав лишитися порожнім"
