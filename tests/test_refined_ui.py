"""DupScan 2.1 source unification and visible-command contracts."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractItemView, QApplication, QPushButton  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


class _SelectionModel:
    def __init__(self, values):
        self.values = values

    def isDir(self, index):
        return self.values[index][1]

    def filePath(self, index):
        return self.values[index][0]


def test_mixed_volume_and_folder_selection_keeps_one_ordered_source_list():
    model = _SelectionModel({
        "volume": ("/Volumes/Studio SSD", True),
        "folder": ("/Users/example/Pictures", True),
        "file": ("/Users/example/Pictures/image.jpg", False),
        "again": ("/Volumes/Studio SSD", True),
    })

    paths = app.directory_paths_from_selection(
        model, ["volume", "folder", "file", "again"])

    assert paths == ["/Volumes/Studio SSD", "/Users/example/Pictures"]


def test_source_picker_is_one_extended_directory_tree():
    dialog = app.SourcePickerDialog()
    dialog.show()
    QTest.qWait(250)

    assert dialog.objectName() == "sourcePickerDialog"
    assert dialog.model.rootPath() == "/"
    root = dialog.tree.rootIndex()
    visible_roots = {
        dialog.model.filePath(dialog.model.index(row, 0, root))
        for row in range(dialog.model.rowCount(root))
    }
    assert visible_roots == {"/Users", "/Volumes"}
    assert (
        dialog.tree.selectionMode()
        == QAbstractItemView.SelectionMode.ExtendedSelection
    )
    assert (
        dialog.tree.selectionBehavior()
        == QAbstractItemView.SelectionBehavior.SelectRows
    )
    assert not dialog.accept_button.isEnabled()
    assert "папки й диски" in dialog.tree.accessibleName().casefold()
    dialog.close()
    dialog.deleteLater()
    _qapp.processEvents()


def test_add_sources_routes_folders_and_volumes_through_one_action(
        tmp_path, monkeypatch):
    folder = str(tmp_path / "Pictures")
    volume = "/Volumes/Studio SSD"
    picked = [folder, volume]
    routed = []
    monkeypatch.setattr(app, "pick_dirs", lambda *_args, **_kwargs: picked)
    main = app.Main()
    monkeypatch.setattr(main, "_add_dirs_async", routed.append)

    main.add_dirs()

    assert routed == [picked]
    assert main.b_add_sources.text() == "Додати джерела…"
    assert not hasattr(main, "b_disk")
    main.close()


def test_results_use_plain_ellipsis_buttons_without_menu_chevrons():
    result = core.ScanResult(
        file_groups=[core.FileGroup(100, "f", ["/a", "/b"])],
        files_seen=2,
        live=True,
    )
    main = app.Main()
    main.on_done(result)
    _qapp.processEvents()

    for model in (main.m_files, main.m_dirs):
        select = main._selection_menu_buttons[model]
        view = main._view_menu_buttons[model]
        assert isinstance(select, QPushButton)
        assert isinstance(view, QPushButton)
        assert select.text().endswith("…")
        assert view.text().endswith("…")
        assert select.menu() is None
        assert view.menu() is None
    assert main.b_clear_similarity_marks.text() == "Зняти позначки"
    main.close()


def test_problems_are_represented_once_inside_review_queue():
    result = core.ScanResult(
        file_groups=[core.FileGroup(100, "f", ["/a", "/b"])],
        errors=["/offline: unavailable"],
        files_seen=2,
        live=True,
    )
    main = app.Main()
    main.on_done(result)
    _qapp.processEvents()

    assert main.findChild(QPushButton, "problemsButton") is None
    assert "Черга перевірки (" in main.b_review_queue.text()
    main.close()
