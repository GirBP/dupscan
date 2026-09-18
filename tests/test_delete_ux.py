"""Deletion must be discoverable without weakening the safe Trash pipeline."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _main_with_duplicates(tmp_path):
    _make(tmp_path / "A" / "same.bin", b"duplicate-data")
    _make(tmp_path / "B" / "same.bin", b"duplicate-data")
    result = core.scan([str(tmp_path)])
    main = app.Main()
    main.result = result
    main.m_files.set_groups(result.file_groups)
    parent = main.m_files.index(0, 0)
    child = main.m_files.index(0, 0, parent)
    main.v_files.setCurrentIndex(child)
    _qapp.processEvents()
    main._update_action_state()
    return main, child, result.file_groups[0].paths[0]


def test_blue_current_row_has_a_native_menu_trash_command(tmp_path, monkeypatch):
    main, _child, current_path = _main_with_duplicates(tmp_path)
    routed = []
    monkeypatch.setattr(
        main, "_delete_duplicate_paths",
        lambda model, paths: routed.append((model, paths)),
    )

    assert main.action_trash_current.isEnabled()
    assert "вибраний дублікат" in main.action_trash_current.text().lower()
    assert main.action_trash_current.shortcut() == QKeySequence("Meta+Backspace")
    assert not main.m_files.checked, "синій рядок не повинен удавати пакетну позначку"

    main.action_trash_current.trigger()
    assert routed == [(main.m_files, {current_path})]
    main.close()


def test_marked_batch_count_and_button_are_unambiguous(tmp_path, monkeypatch):
    main, child, current_path = _main_with_duplicates(tmp_path)
    routed = []
    monkeypatch.setattr(main, "delete_checked", lambda model: routed.append(model))

    assert main.m_files.setData(child, Qt.Checked, Qt.CheckStateRole)
    _qapp.processEvents()
    button = main.b_trash_marked
    assert button.isEnabled()
    assert "пакет (1)" in button.text().lower()
    assert "позначено до кошика: 1" in main.selection_summary.text().lower()
    assert not main.selection_bar.isHidden()
    assert "синій рядок" in main.selection_summary.toolTip().lower()
    assert main.action_trash_marked.isEnabled()

    main.action_trash_marked.trigger()
    assert routed == [main.m_files]
    assert current_path in main.m_files.checked
    main.close()


def test_check_state_commands_are_idempotent():
    paths = ["/tmp/a", "/tmp/b"]
    model = app.GroupModel(lambda _path: (0, 0))
    model.set_groups([core.FileGroup(10, "digest", paths)])
    parent = model.index(0, 0)
    first = model.index(0, 0, parent)

    assert model.setData(first, Qt.Checked, Qt.CheckStateRole)
    assert model.setData(first, Qt.Checked, Qt.CheckStateRole)
    assert model.checked == {paths[0]}
    assert model.setData(first, Qt.Unchecked, Qt.CheckStateRole)
    assert model.setData(first, Qt.Unchecked, Qt.CheckStateRole)
    assert model.checked == set()


def test_exact_result_views_expose_context_menus():
    main = app.Main()
    assert main.v_files.contextMenuPolicy() == Qt.CustomContextMenu
    assert main.v_dirs.contextMenuPolicy() == Qt.CustomContextMenu
    main.close()
