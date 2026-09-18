"""Тека-еталон: недоторканна за визначенням.

Концепт dupeGuru, якого немає в живих macOS-конкурентах: позначена тека
захищена від БУДЬ-ЯКОГО деструктиву на всіх шарах (defense in depth,
шість точок), а не лише «не пропонується до видалення». Порівняння —
realpath обох боків: обхід через symlink на еталон не проходить.

Fail-closed: якщо список еталонів не читається (зіпсований store) —
to_trash відмовляє ВСІМ шляхам із поясненням, а не тихо втрачає захист.
"""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
import dupscan.infra.preferences as preferences  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


# --- сховище і предикат ----------------------------------------------------


def test_roots_round_trip_and_realpath_normalization(tmp_path):
    store = str(tmp_path / "prefs")
    real = tmp_path / "master"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    preferences.save_reference_roots([str(alias)], base_dir=store)

    roots = preferences.load_reference_roots(base_dir=store)
    assert roots == (str(real.resolve()),)  # збережено realpath, не symlink


def test_is_protected_covers_descendants_and_resists_symlink_bypass(tmp_path):
    master = tmp_path / "master"
    (master / "inner").mkdir(parents=True)
    (master / "inner" / "f.bin").write_bytes(b"x")
    outside = tmp_path / "outside"
    outside.mkdir()
    door = outside / "door"
    door.symlink_to(master / "inner")
    roots = (str(master.resolve()),)

    assert preferences.is_protected(str(master / "inner" / "f.bin"), roots)
    assert preferences.is_protected(str(master), roots)
    # шлях ЧЕРЕЗ symlink веде всередину еталона → захищений (realpath)
    assert preferences.is_protected(str(door / "f.bin"), roots)
    assert not preferences.is_protected(str(outside), roots)


def test_unmark_removes_only_named_root(tmp_path):
    store = str(tmp_path / "prefs")
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    preferences.save_reference_roots([str(a), str(b)], base_dir=store)

    preferences.save_reference_roots(
        [r for r in preferences.load_reference_roots(base_dir=store)
         if r != str(a.resolve())],
        base_dir=store)

    assert preferences.load_reference_roots(base_dir=store) == (str(b.resolve()),)


# --- точка 5: останній рубіж to_trash --------------------------------------


def test_to_trash_refuses_protected_paths_last_line(tmp_path, monkeypatch):
    master = tmp_path / "master"
    master.mkdir()
    victim = master / "keep.bin"
    victim.write_bytes(b"sacred")
    free = tmp_path / "free.bin"
    free.write_bytes(b"ok")
    store = str(tmp_path / "prefs")
    preferences.save_reference_roots([str(master)], base_dir=store)
    monkeypatch.setenv("DUPSCAN_DATA_DIR", store)

    errors = fsops.to_trash([str(victim), str(free)])

    assert any("еталон" in e for e in errors), errors
    assert victim.exists(), "захищений файл мусив лишитись на місці"
    assert not free.exists(), "незахищений сусід мав піти в Кошик як завжди"


def test_to_trash_fails_closed_when_roots_unreadable(tmp_path, monkeypatch):
    store = tmp_path / "prefs"
    store.mkdir()
    (store / "reference_roots.json").write_text("{зіпсовано")
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(store))
    target = tmp_path / "f.bin"
    target.write_bytes(b"x")

    errors = fsops.to_trash([str(target)])

    assert errors and "еталон" in errors[0]
    assert target.exists()


# --- точки 2-4: пайплайни app ----------------------------------------------


def _main_with_group(tmp_path, monkeypatch, protected_dir_name: str):
    master = tmp_path / protected_dir_name
    other = tmp_path / "other"
    master.mkdir()
    other.mkdir()
    (master / "dup.bin").write_bytes(b"same-bytes")
    (other / "dup.bin").write_bytes(b"same-bytes")
    store = str(tmp_path / "prefs")
    preferences.save_reference_roots([str(master)], base_dir=store)
    # monkeypatch, НЕ os.environ: голий запис тік у сусідні тести і
    # маскував порядко-залежні збої (спіймано на повному гейті).
    monkeypatch.setenv("DUPSCAN_DATA_DIR", store)
    result = core.scan([str(master), str(other)])
    main = app_mod.Main()
    main.result = result
    main.m_files.set_groups(result.file_groups)
    return main, master, other


def test_delete_pipeline_refuses_protected_victim(tmp_path, monkeypatch):
    main, master, _other = _main_with_group(tmp_path, monkeypatch, "master")
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _p, _t, text, *a, **k: warnings.append(text))
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash", lambda paths, **_k: (trashed.extend(paths), [])[1])

    main._delete_duplicate_paths(main.m_files, {str(master / "dup.bin")})

    assert trashed == []
    assert any("еталон" in w for w in warnings), warnings
    main.close()


def test_merge_refuses_protected_source_before_plan(tmp_path, monkeypatch):
    main, master, other = _main_with_group(tmp_path, monkeypatch, "master")
    pair = core.SimPair(str(master), str(other), 100.0, 1)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    started: list = []
    monkeypatch.setattr(
        main, "_verify_session_pair_then_merge",
        lambda *a, **k: started.append(a))
    monkeypatch.setattr(
        app_mod.core, "merge_plan",
        lambda *a, **k: started.append("plan") or ([], 0))

    # into_a=False: джерело = A = master (еталон) → відмова ДО плану
    main._sim_merge(pair, into_a=False)

    assert started == [], "злиття з еталона мусило відмовити до будь-чого"
    assert "еталон" in main.status.text()
    main.close()


def test_merge_into_protected_target_is_allowed(tmp_path, monkeypatch):
    """В еталон ДОКЛАДАТИ безпечно — захищене лише його спустошення."""
    main, master, other = _main_with_group(tmp_path, monkeypatch, "master")
    pair = core.SimPair(str(master), str(other), 100.0, 1)
    reached: list = []
    monkeypatch.setattr(
        main, "_run_sim_merge", lambda *a, **k: reached.append(a))

    main._sim_merge(pair, into_a=True)  # ціль A = master, джерело B = other

    assert reached, "ціль-еталон не мала блокувати злиття"
    main.close()


def test_remove_shared_refuses_protected_side(tmp_path, monkeypatch):
    main, master, other = _main_with_group(tmp_path, monkeypatch, "master")
    pair = core.SimPair(str(master), str(other), 100.0, 1)
    flows: list = []
    monkeypatch.setattr(
        main, "_sim_trash_flow", lambda *a, **k: flows.append(a))

    main._sim_remove_shared(pair, remove_from_a=True)  # бік A = master

    assert flows == []
    assert "еталон" in main.status.text()
    main.close()


# --- точка 1: чекбокс у моделі ---------------------------------------------


def test_model_checkbox_refuses_protected_path(tmp_path, monkeypatch):
    main, master, _other = _main_with_group(tmp_path, monkeypatch, "master")
    model = main.m_files
    warned: list[str] = []
    model.warn = warned.append
    index = None
    for row in range(model.rowCount()):
        group_index = model.index(row, 0)
        for child in range(model.rowCount(group_index)):
            candidate = model.index(child, 0, group_index)
            if model._path(candidate) == str(master / "dup.bin"):
                index = candidate
    assert index is not None

    ok = model.setData(index, Qt.CheckState.Checked.value, Qt.CheckStateRole)

    assert not ok
    assert str(master / "dup.bin") not in model.checked
    assert warned and "еталон" in warned[0]
    main.close()


# --- точка 6: автовибір ----------------------------------------------------


def test_auto_selection_never_marks_protected_paths(tmp_path):
    master = tmp_path / "master"
    other = tmp_path / "other"
    master.mkdir()
    other.mkdir()
    keeper_rules = preferences.SelectionRules(keep="lexical")
    candidates = [str(master / "a.bin"), str(other / "b.bin")]

    victims = preferences.select_victims(
        candidates, keeper_rules,
        reference_roots=(str(master.resolve()),))

    assert str(master / "a.bin") not in victims
