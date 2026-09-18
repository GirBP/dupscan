"""«Перевірити й прибрати» для ОДНІЄЇ групи зі знімка.

Дзеркало парного механізму (_verify_session_pair_then_action /
_sim_trash_flow(verified_result=...)): historical/partial результат
більше не отримує німу заборону для груп — свіже ПОВНЕ читання РІВНО
членів зачепленої групи (незалежно від file_meta знімка) доводить, що
принаймні один непозначений член лишається живою незалежною копією, і
лише тоді звичайний ланцюг (ReviewDialog -> підтвердження ->
to_trash(expected_identities) -> історія) відправляє жертву в Кошик.
Сам знімок (ScanResult у пам'яті) НІКОЛИ не мутується — деструктив без
свіжого доказу обмеженої області заборонений, а знімок незмінний
(знімок незмінний за побудовою).
"""

import os
import sys
import tempfile
import time
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def make(p, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def wait_until(cond, timeout=8.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        _qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _yes(monkeypatch) -> None:
    """Auto-confirm every QMessageBox.question/.warning: under offscreen
    Qt, .exec() still opens a REAL local event loop waiting for a click
    that can never come — unmocked, it hangs the test forever, not just
    fails. Stale-item warnings are expected informational noise in most
    of these scenarios, not something under test here."""
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda *a, **k: QMessageBox.StandardButton.Ok)


def _record_trash(monkeypatch) -> list:
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_k: (trashed.extend(paths), [])[1])
    return trashed


def _historical_file_group(tmp_path, *, third=False):
    """Тека з файловою групою (A/B, опційно C) -> завантажена сесія (live=False)."""
    tree = tmp_path / "tree"
    payload = os.urandom(4096)
    make(tree / "A" / "f.bin", payload)
    make(tree / "B" / "f.bin", payload)
    if third:
        make(tree / "C" / "f.bin", payload)
    result = core.scan([str(tree)])
    saved = session.save_session(result, [str(tree)], base_dir=str(tmp_path / "data"))
    return tree, session.load_session(saved)


def _historical_dir_group(tmp_path):
    tree = tmp_path / "tree"
    payload = os.urandom(4096)
    make(tree / "A" / "f.bin", payload)
    make(tree / "B" / "f.bin", payload)
    result = core.scan([str(tree)])
    saved = session.save_session(result, [str(tree)], base_dir=str(tmp_path / "data"))
    return tree, session.load_session(saved)


# ================================ A1 ========================================


def test_a1_historical_file_group_fresh_proof_trashes_victim_survivor_alive(
        tmp_path, monkeypatch):
    tree, loaded = _historical_file_group(tmp_path)
    assert loaded.live is False
    historical_state = deepcopy(loaded)
    group = loaded.file_groups[0]
    victim, survivor = group.paths[0], group.paths[1]

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_files, {victim})

    assert wait_until(lambda: trashed == [victim])
    assert os.path.exists(survivor), "жива копія має лишитися на диску"
    assert loaded == historical_state, "історична сесія має лишитися незмінною"
    main.close()


# ================================ A2 ========================================


def test_a2_victim_mutated_after_session_saved_is_skipped_trash_stays_empty(
        tmp_path, monkeypatch):
    tree, loaded = _historical_file_group(tmp_path)
    group = loaded.file_groups[0]
    victim, survivor = group.paths[0], group.paths[1]
    # Жертва змінилась НА ДИСКУ після збереження сесії.
    with open(victim, "wb") as fh:
        fh.write(os.urandom(4096))

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_files, {victim})

    assert wait_until(lambda: main.status.text() == "Нічого безпечно видаляти.")
    assert trashed == []
    assert os.path.exists(victim) and os.path.exists(survivor)
    main.close()


# ================================ A3 ========================================


def test_a3_survivor_vanished_before_verify_refuses_whole_action(
        tmp_path, monkeypatch):
    tree, loaded = _historical_file_group(tmp_path)
    group = loaded.file_groups[0]
    victim, survivor = group.paths[0], group.paths[1]
    os.remove(survivor)  # єдиний кандидат-«survivor» зник з диска

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_files, {victim})

    assert wait_until(lambda: main.status.text() == "Нічого безпечно видаляти.")
    assert trashed == [], "нуль деструктиву — жертва теж лишається"
    assert os.path.exists(victim), "жертва без доведеного survivor-а не видаляється"
    main.close()


# ================================ A4 ========================================


def test_a4_selecting_every_member_refuses_before_worker_starts(
        tmp_path, monkeypatch):
    tree, loaded = _historical_file_group(tmp_path)
    group = loaded.file_groups[0]
    all_members = set(group.paths)

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)
    read_paths: list[str] = []
    real_verify = core.verify_current_file

    def spy(path, *a, **k):
        read_paths.append(path)
        return real_verify(path, *a, **k)

    monkeypatch.setattr(core, "verify_current_file", spy)

    main._verify_snapshot_group_then_trash(main.m_files, all_members)
    _qapp.processEvents()

    assert trashed == []
    assert read_paths == [], "воркер не мав навіть стартувати"
    for p in all_members:
        assert os.path.exists(p)
    main.close()


# ================================ A5 ========================================


def test_a5_reads_exactly_the_affected_groups_members(tmp_path, monkeypatch):
    tree, loaded = _historical_file_group(tmp_path)
    # Друга, НЕ зачеплена група в тій самій сесії/моделі.
    unrelated_payload = os.urandom(4096)
    make(tree / "unrelated-1" / "u.bin", unrelated_payload)
    make(tree / "unrelated-2" / "u.bin", unrelated_payload)
    full_result = core.scan([str(tree)])
    saved = session.save_session(
        full_result, [str(tree)], base_dir=str(tmp_path / "data2"))
    loaded = session.load_session(saved)
    target_group = next(
        g for g in loaded.file_groups
        if os.path.basename(g.paths[0]) == "f.bin")
    unrelated_group = next(
        g for g in loaded.file_groups
        if os.path.basename(g.paths[0]) == "u.bin")
    victim = target_group.paths[0]

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)
    read_paths: list[str] = []
    real_verify = core.verify_current_file

    def spy(path, *a, **k):
        read_paths.append(path)
        return real_verify(path, *a, **k)

    monkeypatch.setattr(core, "verify_current_file", spy)

    main._verify_snapshot_group_then_trash(main.m_files, {victim})

    assert wait_until(lambda: trashed == [victim])
    assert set(read_paths) == set(target_group.paths), (
        f"мали читатись рівно члени зачепленої групи: {sorted(read_paths)}")
    assert not (set(read_paths) & set(unrelated_group.paths)), (
        "сусідня, не зачеплена група не мала читатися")
    main.close()


# ================================ A6 ========================================


def test_a6_directory_group_positive_and_negative(tmp_path, monkeypatch):
    tree, loaded = _historical_dir_group(tmp_path)
    group = loaded.dir_groups[0]
    victim, survivor = group.paths[0], group.paths[1]

    main = app_mod.Main()
    main.result = loaded
    main.m_dirs.set_groups(loaded.dir_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_dirs, {victim})

    assert wait_until(lambda: trashed == [victim])
    assert os.path.isdir(survivor)
    main.close()


def test_a6_directory_group_negative_changed_survivor_refuses(tmp_path, monkeypatch):
    tree, loaded = _historical_dir_group(tmp_path)
    group = loaded.dir_groups[0]
    victim, survivor = group.paths[0], group.paths[1]
    # Тека-«survivor» змінилась НА ДИСКУ після знімка — більше не пара.
    make(tree / os.path.basename(survivor) / "extra.bin", os.urandom(128))

    main = app_mod.Main()
    main.result = loaded
    main.m_dirs.set_groups(loaded.dir_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_dirs, {victim})

    assert wait_until(lambda: main.status.text() == "Нічого безпечно видаляти.")
    assert trashed == []
    assert os.path.isdir(victim)
    main.close()


# ================================ A7 ========================================


def test_a7_imported_session_group_path_works_direct_delete_still_forbidden(
        tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    payload = os.urandom(4096)
    make(tree / "A" / "f.bin", payload)
    make(tree / "B" / "f.bin", payload)
    scanned = core.scan([str(tree)])
    export_source = session.save_session(
        scanned, [str(tree)], base_dir=str(tmp_path / "export-data"))
    exported = tmp_path / "backup.json"
    session.export_session(export_source, str(exported))
    imported_path = session.import_session(
        str(exported), base_dir=str(tmp_path / "import-data"))
    imported = session.load_session(imported_path)
    assert imported.live is False
    group = imported.file_groups[0]
    victim, survivor = group.paths[0], group.paths[1]

    main = app_mod.Main()
    main.result = imported
    main.m_files.set_groups(imported.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_files, {victim})
    assert wait_until(lambda: trashed == [victim])
    assert os.path.exists(survivor)

    # Регресія: ПРЯМИЙ шлях (без свіжої перевірки) лишається забороненим.
    trashed.clear()
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *a, **_k: messages.append(a[-1]))
    main.m_files.checked.add(survivor)
    main.delete_checked(main.m_files)
    assert trashed == []
    assert messages and "лише для перегляду" in messages[-1]
    main.close()


# ================================ A8 ========================================


def test_a8_view_tag_after_success_and_byte_identical_snapshot(tmp_path, monkeypatch):
    tree, loaded = _historical_file_group(tmp_path)
    historical_state = deepcopy(loaded)
    group = loaded.file_groups[0]
    victim = group.paths[0]

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    _yes(monkeypatch)
    trashed = _record_trash(monkeypatch)

    main._verify_snapshot_group_then_trash(main.m_files, {victim})

    assert wait_until(lambda: trashed == [victim])
    assert main.m_files.tags.get(victim), "рядок жертви мусить отримати мітку у view"
    assert loaded.file_groups == historical_state.file_groups, (
        "file_groups знімка мають лишитися байт-у-байт ті самі")
    assert loaded == historical_state
    main.close()
