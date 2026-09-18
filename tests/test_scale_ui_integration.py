"""Integrated large-result controls without touching the filesystem."""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402


_qapp = QApplication.instance() or QApplication([])


def _group_model() -> app.GroupModel:
    dates = {
        "/local/a": (1, 1),
        "/local/b": (1, 2),
        "/Volumes/NAS/a": (1, 1),
        "/Volumes/NAS/b": (1, 2),
    }
    model = app.GroupModel(dates.get)
    model.set_groups([
        core.FileGroup(10, "local", ["/local/a", "/local/b"]),
        core.FileGroup(
            20, "external", ["/Volumes/NAS/a", "/Volumes/NAS/b"]),
        core.FileGroup(30, "missing", ["/missing/a", "/missing/b"]),
    ])
    return model


def test_presets_filter_in_memory_without_losing_checked_paths():
    model = _group_model()
    model.checked.add("/local/b")

    model.set_view_preset("network")
    assert model.rowCount() == 1
    assert model.root_id(0) == 1
    assert model.checked == {"/local/b"}
    model.select_filtered_safe()
    assert model.checked == {"/local/b", "/Volumes/NAS/b"}
    assert not any(path.startswith("/missing/") for path in model.checked)

    model.set_view_preset("safe")
    assert model.rowCount() == 2
    assert {model.root_id(row) for row in range(model.rowCount())} == {0, 1}
    assert model.checked == {"/local/b", "/Volumes/NAS/b"}

    model.set_view_preset("review")
    assert {model.root_id(row) for row in range(model.rowCount())} == {1, 2}

    model.set_view_preset("invented")
    assert model._view_preset == "all"
    assert model.rowCount() == 3


def test_density_inspector_and_filter_state_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    first = app.Main()
    assert first.v_files.property("dupscanDensity") == "compact"
    assert not first._inspectors[first.m_files].isVisible()

    first._density_buttons[first.m_files].setChecked(False)
    first._inspector_buttons[first.m_files].setChecked(True)
    first._search_boxes[first.m_files].setText("invoice")
    first._category_boxes[first.m_files].setCurrentIndex(
        first._category_boxes[first.m_files].findData("document"))
    first._preset_boxes[first.m_files].setCurrentIndex(
        first._preset_boxes[first.m_files].findData("largest"))
    first._save_view_settings()
    first.deleteLater()

    second = app.Main()
    assert second.v_files.property("dupscanDensity") == "comfortable"
    assert second._inspector_buttons[second.m_files].isChecked()
    assert second._search_boxes[second.m_files].text() == "invoice"
    assert second._category_boxes[second.m_files].currentData() == "document"
    assert second._preset_boxes[second.m_files].currentData() == "largest"
    second.deleteLater()


def test_keyboard_review_shortcuts_are_single_action_and_scope_safe(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    main = app.Main()
    paths = ["/root/match-a", "/root/match-b", "/root/hidden"]
    main.m_files.set_groups([core.FileGroup(10, "g", paths)])
    main.m_files.set_filter("match")
    parent = main.m_files.index(0, 0)
    main.v_files.expand(parent)
    child = main.m_files.index(0, 0, parent)
    main.v_files.setCurrentIndex(child)
    main.show()
    main.v_files.setFocus()
    _qapp.processEvents()

    QTest.keyClick(main.v_files, Qt.Key_Space)
    assert len(main.m_files.checked) == 1
    QTest.keyClick(main.v_files, Qt.Key_Space)
    assert main.m_files.checked == set()

    QTest.keyClick(main.v_files, Qt.Key_Slash)
    assert main._search_boxes[main.m_files].hasFocus()
    main._search_boxes[main.m_files].setText("match")
    QTest.keyClick(main._search_boxes[main.m_files], Qt.Key_Escape)
    assert main._search_boxes[main.m_files].text() == ""

    was_visible = main._inspector_buttons[main.m_files].isChecked()
    QTest.keyClick(main, Qt.Key_I, Qt.MetaModifier)
    assert main._inspector_buttons[main.m_files].isChecked() is not was_visible

    main.m_files.set_filter("match")
    QTest.keyClick(
        main, Qt.Key_A, Qt.MetaModifier | Qt.ShiftModifier)
    assert main.m_files.checked == {"/root/match-a", "/root/match-b"}
    assert "/root/hidden" not in main.m_files.checked
    main.close()


def test_review_queue_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    main = app.Main()
    main.m_files.set_groups([
        core.FileGroup(10, "g", ["/a", "/b"]),
    ])
    main.m_files.checked.add("/b")
    before = set(main.m_files.checked)
    monkeypatch.setattr(QDialog, "exec", lambda _dialog: QDialog.Rejected)

    main.show_review_queue()

    assert main.m_files.checked == before
    assert main.b_review_queue.accessibleName()
    main.deleteLater()


def test_group_roots_are_paged_but_filtered_batch_scope_covers_all():
    groups = [
        core.FileGroup(10, f"g-{index}", [
            f"/root/{index}/keep", f"/root/{index}/copy"])
        for index in range(app.ROOT_PAGE_SIZE * 2 + 25)
    ]
    model = app.GroupModel(lambda _path: (1, 1))
    model.keeper_fn = lambda paths: paths[0]
    model.set_groups(groups)

    assert model.rowCount() == app.ROOT_PAGE_SIZE + 1
    sentinel = model.index(app.ROOT_PAGE_SIZE, 0)
    assert "Показати ще" in model.data(sentinel, Qt.DisplayRole)
    assert model.load_more_at(sentinel)
    assert model.rowCount() == app.ROOT_PAGE_SIZE * 2 + 1

    model.select_filtered_safe()
    assert len(model.checked) == len(groups)
    assert all(path.endswith("/copy") for path in model.checked)


def test_similarity_roots_and_cells_are_lazy_and_paged():
    pairs = [
        core.SimPair(
            f"/a/{index}", f"/b/{index}", 80.0, 10,
            [(10, f"/a/{index}/same", f"/b/{index}/same")],
            1,
        )
        for index in range(app.ROOT_PAGE_SIZE * 2 + 1)
    ]
    model = app.SimModel()
    model.set_pairs(pairs)

    assert model.rowCount() == app.ROOT_PAGE_SIZE + 1
    assert model._cells == {}
    assert model._rowmap == {}
    model.sort(2, Qt.DescendingOrder)
    assert model._cells == {}
    assert model._rowmap == {}

    parent = model.index(0, 0)
    assert model.rowCount(parent) == 1
    assert len(model._rowmap) == 1
    child = model.index(0, 0, parent)
    assert model.data(child, Qt.DisplayRole) == "same"
    assert len(model._cells) == 1

    sentinel = model.index(app.ROOT_PAGE_SIZE, 0)
    assert model.load_more_at(sentinel)
    assert model.rowCount() == app.ROOT_PAGE_SIZE * 2 + 1


def test_large_result_admission_runs_off_gui_and_finishes_paged(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    groups = [
        core.FileGroup(
            10, f"g-{index}",
            [f"/root/{index}/a", f"/root/{index}/b"],
        )
        for index in range(12_000)
    ]
    result = core.ScanResult(
        file_groups=groups, files_seen=24_000, bytes_seen=240_000)
    main = app.Main()
    main.result = result

    started = time.perf_counter()
    main._refresh_models()
    admission = time.perf_counter() - started

    assert admission < 0.1
    assert main._model_preparing
    heartbeat = False
    deadline = time.monotonic() + 4
    while main._model_preparing and time.monotonic() < deadline:
        _qapp.processEvents()
        heartbeat = True
    assert heartbeat
    assert not main._model_preparing
    assert len(main.m_files.groups) == len(groups)
    assert main.m_files.rowCount() == app.ROOT_PAGE_SIZE + 1
    main.close()


def test_superseded_large_model_preparation_cannot_replace_new_result(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    old = core.ScanResult(file_groups=[
        core.FileGroup(10, f"old-{index}", [
            f"/old/{index}/a", f"/old/{index}/b"])
        for index in range(12_000)
    ])
    new_group = core.FileGroup(20, "new", ["/new/a", "/new/b"])
    new = core.ScanResult(file_groups=[new_group])
    main = app.Main()
    main.result = old
    main._refresh_models()
    assert main._model_preparing

    main.result = new
    main._refresh_models()
    deadline = time.monotonic() + 4
    while main._ui_bg and time.monotonic() < deadline:
        _qapp.processEvents()

    assert main.result is new
    assert main.m_files.groups == [new_group]
    assert main.m_files.data(main.m_files.index(0, 0)).startswith("Група 1")
    main.close()


def test_large_batch_selection_yields_and_keeps_one_survivor_per_group(
        tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    groups = [
        core.FileGroup(10, f"g-{index}", [
            f"/root/{index}/keep", f"/root/{index}/copy"])
        for index in range(12_000)
    ]
    main = app.Main()
    main.m_files.keeper_fn = lambda paths: paths[0]
    main.m_files.set_groups(groups)

    started = time.perf_counter()
    main._select_model_scope(main.m_files, "all_safe")
    admission = time.perf_counter() - started

    assert admission < 0.1
    assert main._batch_selecting
    deadline = time.monotonic() + 4
    while main._batch_selecting and time.monotonic() < deadline:
        _qapp.processEvents()
    assert not main._batch_selecting
    assert len(main.m_files.checked) == len(groups)
    assert all(path.endswith("/copy") for path in main.m_files.checked)
    main.close()
