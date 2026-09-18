"""E2E «двійник власника»: повний ланцюг злиття copy→downloads
через ті самі воркери, якими користується GUI, без вікна Main.

Дерево — qa.make_owner_tree.build_owner_tree(): службові теки
(__pycache__/.git), кириличні імена, 3+ пари справжніх дублікатів,
унікальні файли з обох боків, symlink, hardlink-пара, файл 0 байтів,
колізія імені у цілі. Це та сама структура, яка на BARRACUDA власника
відмовляла діалогом «Не всі елементи у вибраних теках вдалося
прочитати…» і — як показав цей самий E2E-прогін — ще й
ламала MergePreparationWorker/_transfer_files на кожному symlink
(окремий дефект, полагоджений тут же: core.verify_current_symlink,
tests/test_symlink_merge_transfer.py).

Ізоляція ПЕРЕД імпортом Qt: DUPSCAN_DATA_DIR, QT_QPA_PLATFORM — інакше
тест торкнеться реальної сесії/кешу користувача. HOME НЕ підмінюється на
рівні модуля: pytest імпортує (тобто виконує) усі тестові файли на етапі
збору ЩЕ до запуску будь-якого тесту, тож жорстке os.environ["HOME"]=…
тут просочувалося б у ВЕСЬ прогін і ламало інші файли, які покладаються
на реальний HOME (напр. test_refined_ui.py очікує "/Users" серед видимих
коренів). Цей сценарій свідомо БЕЗ Main() — жоден QSettings об'єкт не
створюється, тож підміна HOME тут не потрібна на рівні модуля; де вона
все ж бажана про всяк випадок, кожен тест підміняє її monkeypatch-ем
(автоматично відновлюється після тесту, без просочування).
"""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402
import dupscan.ui.workers as workers  # noqa: E402
from qa.make_owner_tree import build_owner_tree  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _run_scan(base) -> workers.ScanWorker:
    worker = workers.ScanWorker([str(base)])
    failed = []
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, f"ScanWorker не мав падати: {failed}"
    return worker


def _run_pair_verification(dir_a: str, dir_b: str) -> core.ScanResult:
    worker = workers.PairVerificationWorker(dir_a, dir_b)
    results = []
    failed = []
    worker.done.connect(results.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, f"PairVerificationWorker не мав падати: {failed}"
    assert len(results) == 1
    return results[0]


def _run_merge_prep(result: core.ScanResult, src: str, dst: str):
    worker = workers.MergePreparationWorker(result, src, dst)
    results = []
    failed = []
    worker.done.connect(results.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert not failed, f"MergePreparationWorker не мав падати: {failed}"
    assert len(results) == 1 and results[0] is not None
    return results[0]


def test_owner_scenario_move_merge_and_trash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    base = tmp_path / "owner"
    paths = build_owner_tree(base)
    downloads = paths["downloads"]
    copy = paths["copy"]

    # ---- 1. ScanWorker([base]).run() синхронно, як робить GUI -------------
    scan_worker = _run_scan(base)
    assert scan_worker.saved_session_path

    # ---- 2. Сесія збереглась, session.load_session вантажить її ----------
    loaded = session.load_session(scan_worker.saved_session_path)
    assert not loaded.partial
    assert loaded.errors == []

    # ---- 3. Повний ланцюг злиття copy → downloads -------------------------
    fresh = _run_pair_verification(str(copy), str(downloads))
    assert not fresh.partial
    assert core.directory_tree_mergeable(fresh, str(copy))
    assert core.directory_tree_mergeable(fresh, str(downloads))
    # directory_tree_complete лишається суворим (Merkle-докази точних
    # копій тек) — саме __pycache__/.git у copy мали б його провалити:
    assert not core.directory_tree_complete(fresh, str(copy))

    plan, total, expected_digests, same_device, root_identities = (
        _run_merge_prep(fresh, str(copy), str(downloads)))
    assert plan  # є що переносити (уся некопійована частина)
    assert total >= 0

    # Захоплюємо очікуваний вміст ДО переносу — джерело зникне.
    collision_copy_bytes = paths["collision_copy_path"].read_bytes()
    collision_downloads_bytes = paths["collision_downloads_path"].read_bytes()
    hardlink_bytes = paths["hardlink_a_path"].read_bytes()
    pycache_bytes = paths["pycache_path"].read_bytes()
    symlink_target = os.readlink(paths["symlink_path"])

    # ---- 4. app._move_files(...) з expected_digests і root_identities ----
    completed, errors = app_mod._move_files(
        fresh, plan, str(downloads), str(copy),
        expected_digests, root_identities)
    assert errors == [], f"перенос не мав давати помилок: {errors}"
    assert completed

    # ---- 5. У цілі присутні ВСІ неспільні файли ----------------------------
    assert (downloads / paths["pycache_rel"]).exists()
    assert (downloads / paths["pycache_rel"]).read_bytes() == pycache_bytes
    assert (downloads / paths["git_rel"]).exists()
    assert (downloads / paths["cyrillic_copy_rel"]).exists()
    assert (downloads / paths["unique_copy_rel"]).exists()
    # цільові унікальні файли (ніколи не були в copy) лишаються на місці:
    assert (downloads / paths["cyrillic_downloads_rel"]).exists()
    assert (downloads / paths["unique_downloads_rel"]).exists()

    symlink_dst = downloads / paths["symlink_rel"]
    assert symlink_dst.is_symlink()
    assert os.readlink(symlink_dst) == symlink_target

    zero_dst = downloads / paths["zero_byte_rel"]
    assert zero_dst.exists() and zero_dst.stat().st_size == 0

    hardlink_a_dst = downloads / paths["hardlink_a_rel"]
    hardlink_b_dst = downloads / paths["hardlink_b_rel"]
    assert hardlink_a_dst.exists() and hardlink_b_dst.exists()
    assert hardlink_a_dst.read_bytes() == hardlink_bytes
    assert hardlink_b_dst.read_bytes() == hardlink_bytes
    assert hardlink_a_dst.stat().st_ino == hardlink_b_dst.stat().st_ino

    # ---- 6. Колізія: оригінальне ім'я лишає ЦІЛЬОВИЙ вміст, копія — з
    # суфіксом " (2)" ---------------------------------------------------
    collision_name = paths["collision_rel"]
    base_name, ext = os.path.splitext(collision_name)
    original_dst = downloads / collision_name
    suffixed_dst = downloads / f"{base_name} (2){ext}"
    assert original_dst.exists()
    assert original_dst.read_bytes() == collision_downloads_bytes
    assert suffixed_dst.exists()
    assert suffixed_dst.read_bytes() == collision_copy_bytes

    # ---- 7. Дублікати лишаються на місці по обидва боки --------------------
    for rel in paths["duplicate_rels"]:
        assert (downloads / rel).exists()
        assert (copy / rel).exists()

    # ---- 8. app._verify_then_trash_dir(fresh, copy) з to_trash-рекордером -
    trashed: list = []

    def recorder(victims, **_kwargs):
        trashed.extend(victims)
        return []

    monkeypatch.setattr(app_mod, "to_trash", recorder)

    kind, trash_errors = app_mod._verify_then_trash_dir(fresh, str(copy))

    assert (kind, trash_errors) == ("ok", [])
    assert trashed == [str(copy)]


def test_owner_scenario_copy_merge_leaves_source_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    base = tmp_path / "owner"
    paths = build_owner_tree(base)
    downloads = paths["downloads"]
    copy = paths["copy"]

    _run_scan(base)
    fresh = _run_pair_verification(str(copy), str(downloads))
    assert core.directory_tree_mergeable(fresh, str(copy))
    assert core.directory_tree_mergeable(fresh, str(downloads))

    plan, total, expected_digests, same_device, root_identities = (
        _run_merge_prep(fresh, str(copy), str(downloads)))
    assert plan

    # Знімок джерела ДО копіювання — має лишитися побайтово незмінним.
    before_snapshot = {
        os.path.relpath(os.path.join(dirpath, name), copy):
            os.lstat(os.path.join(dirpath, name))
        for dirpath, _dirnames, filenames in os.walk(str(copy))
        for name in filenames
    }

    completed, errors = app_mod._copy_files(
        fresh, plan, str(downloads), str(copy),
        expected_digests, root_identities)

    assert errors == [], f"копіювання не мало давати помилок: {errors}"
    assert completed

    after_snapshot = {
        os.path.relpath(os.path.join(dirpath, name), copy):
            os.lstat(os.path.join(dirpath, name))
        for dirpath, _dirnames, filenames in os.walk(str(copy))
        for name in filenames
    }
    assert set(before_snapshot) == set(after_snapshot), "джерело не мало втратити файли"
    for rel, before in before_snapshot.items():
        after = after_snapshot[rel]
        assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), (
            f"{rel}: джерело не мало змінитися при копіюванні")

    # Ціль отримала неспільний вміст так само, як при move:
    assert (downloads / paths["pycache_rel"]).exists()
    assert (downloads / paths["symlink_rel"]).is_symlink()
    assert (downloads / paths["zero_byte_rel"]).exists()


def test_owner_scenario_stale_plan_fails_closed_after_deletion(tmp_path, monkeypatch):
    """Після MergePreparationWorker («після підготовки») з диска зникає
    один файл, охоплений планом. Повторний PairVerificationWorker — це
    ЧЕСНИЙ незалежний знімок: він не порівнює себе з попереднім станом,
    тому сам по собі зникнення файла (не збій читання, не виключення) не
    лишає слідів у errors/dir_read_failed — це НЕ дефект, а коректна
    семантика "кожен скан — правдивий знімок поточного диска".

    Справжній fail-closed бар'єр — у ПЕРЕНОСІ зі СТАРИМ планом:
    verify_current_file на зниклому шляху отримує OSError (ENOENT) і
    _transfer_files повертає непорожній errors із конкретним шляхом —
    не заглушку. Це і є гейт, що не пускає застарілий план на диск.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    base = tmp_path / "owner"
    paths = build_owner_tree(base)
    downloads = paths["downloads"]
    copy = paths["copy"]

    _run_scan(base)
    fresh1 = _run_pair_verification(str(copy), str(downloads))
    plan, total, expected_digests, same_device, root_identities = (
        _run_merge_prep(fresh1, str(copy), str(downloads)))
    assert plan

    victim = paths["unique_copy_path"]
    assert victim.exists()
    victim_rel = str(victim.relative_to(copy))
    assert any(rel == victim_rel for _size, _src, rel in plan)
    os.remove(victim)

    # Повторний, повністю незалежний PairVerificationWorker: чесний,
    # не крашиться, mergeable лишається True — зникнення файла саме по
    # собі не є збоєм читання.
    fresh2 = _run_pair_verification(str(copy), str(downloads))
    assert not fresh2.partial
    assert core.directory_tree_mergeable(fresh2, str(copy))
    assert core.directory_tree_mergeable(fresh2, str(downloads))

    # Застарілий план (побудований ДО видалення) проти поточного диска:
    # _move_files мусить відмовити на конкретному файлі, не мовчки
    # пропустити його.
    completed, errors = app_mod._move_files(
        fresh1, plan, str(downloads), str(copy),
        expected_digests, root_identities)

    assert errors, "застарілий план на зниклому файлі мав дати помилку"
    assert any(str(victim) in message for message in errors), errors
    assert not any(message.strip() == "" for message in errors)
    # Причина НЕ заглушка: містить шлях жертви і слово помилки ОС.
    culprit = next(message for message in errors if str(victim) in message)
    assert len(culprit) > len(str(victim))
