"""Headless preflight for Qt accessibility and full-path UX contracts."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.domain.product as product  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _long_path(side: str) -> str:
    component = f"{side}-Дуже-довга-назва-теки-資料-" + ("ю" * 90)
    return "/tmp/" + "/".join(f"{component}-{index}" for index in range(5))


def test_accessibility_contract_main_window():
    main = app.Main()
    expected = {
        "scanFolders": "Теки для сканування",
        "scanButton": "Сканувати",
        "pauseButton": "Пауза або продовження",
        "cancelButton": "Скасувати операцію",
        "scanProgress": "Прогрес поточної операції",
        "scanProfile": "Активний профіль сканування",
        "operationStatus": "Стан поточної операції",
        "scanSummary": "Підсумок результатів сканування",
        "resultTabs": "Результати сканування",
        "duplicateFilesTree": "Дублікати файлів",
        "duplicateFoldersTree": "Дублікати папок",
        "similarFoldersTree": "Подібні папки",
        "trashMarkedButton": "Кошик",
        "reviewQueueButton": "Черга результатів",
    }
    for object_name, accessible_fragment in expected.items():
        widget = main.findChild(QWidget, object_name)
        assert widget is not None, object_name
        assert accessible_fragment in widget.accessibleName()
        assert widget.focusPolicy() != Qt.NoFocus or object_name in {
            "operationStatus", "scanSummary", "scanProgress", "scanProfile",
        }

    for object_name in (
            "addSourcesButton", "clearFoldersButton",
            "sessionHistoryButton", "removalHistoryButton",
            "compareFoldersButton", "preferencesButton"):
        button = main.findChild(QPushButton, object_name)
        assert button is not None
        assert button.accessibleName()
        assert button.focusPolicy() != Qt.NoFocus

    assert "Кошик" in main.action_trash_current.text()
    assert "Кошик" in main.action_trash_current.toolTip()
    assert "Кошик" in main.action_trash_marked.text()
    assert "Кошик" in main.action_trash_marked.toolTip()
    main.deleteLater()


def test_long_unicode_paths_are_never_truncated_in_path_contracts():
    path_a = _long_path("A")
    path_b = _long_path("B")

    folders = app.FolderList()
    folders.add_dir(path_a)
    assert folders.item(0).text() == path_a
    assert folders.item(0).toolTip() == path_a

    groups = app.GroupModel(lambda _path: None)
    groups.set_groups([core.FileGroup(10, "digest", [path_a, path_b])])
    parent = groups.index(0, 0)
    child = groups.index(0, 0, parent)
    assert groups.data(child, Qt.DisplayRole) == path_a
    assert groups.data(child, Qt.ToolTipRole) == path_a

    pair = core.SimPair(path_a, path_b, 80.0, 10)
    similarities = app.SimModel()
    similarities.set_pairs([pair])
    root = similarities.index(0, 0)
    tooltip = similarities.data(root, Qt.ToolTipRole)
    assert path_a in tooltip
    assert path_b in tooltip

    comparison = product.FolderComparison(
        path_a, path_b,
        [product.FolderCompareRow(
            "same.bin", "identical",
            path_a=f"{path_a}/same.bin", path_b=f"{path_b}/same.bin")],
    )
    model = app.FolderCompareModel(comparison)
    assert model.data(model.index(0, 2), Qt.ToolTipRole) == f"{path_a}/same.bin"
    assert model.data(model.index(0, 3), Qt.ToolTipRole) == f"{path_b}/same.bin"


def test_review_and_folder_compare_dialog_accessibility_contract():
    path_a = _long_path("A")
    path_b = _long_path("B")
    entry = {
        "path": f"{path_a}/same.bin",
        "category": "other",
        "size": 10,
        "mtime_ns": 0,
        "reason": "Повний BLAKE3 збігається",
    }
    review = app.ReviewDialog(None, [entry], {entry["path"]}, [[entry["path"], path_b]])
    review._show_entry(0)
    assert review.objectName() == "reviewDialog"
    assert review.table.accessibleName() == "Кандидати до Кошика"
    assert review.details.accessibleName() == "Повний шлях і доказ дубліката"
    assert entry["path"] in review.details.text()
    assert review.model.data(review.model.index(0, 3), Qt.ToolTipRole) == entry["path"]
    assert review.b_quick.focusPolicy() != Qt.NoFocus
    assert review.b_reveal.focusPolicy() != Qt.NoFocus

    comparison = product.FolderComparison(path_a, path_b, [])
    dialog = app.FolderCompareDialog(
        None, comparison, merge_callback=lambda *_args: None,
        merge_verification_required=True)
    assert dialog.objectName() == "folderCompareDialog"
    assert dialog.table.accessibleName() == "Результати порівняння тек"
    assert path_a in dialog.summary.text()
    assert path_b in dialog.summary.text()
    assert dialog.findChild(QPushButton, "mergeBToA") is not None
    assert dialog.findChild(QPushButton, "mergeAToB") is not None
    review.deleteLater()
    dialog.deleteLater()
