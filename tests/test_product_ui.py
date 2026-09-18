import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _entry(path, category="document", size=10):
    return {
        "path": path,
        "category": category,
        "size": size,
        "mtime_ns": 0,
        "reason": "Повний BLAKE3 збігається",
    }


def test_review_model_preserves_one_copy_and_updates_summary_signal():
    paths = ["/tmp/a.txt", "/tmp/b.txt", "/tmp/c.txt"]
    model = app.ReviewModel(
        [_entry(path) for path in paths], {paths[0], paths[1]}, [paths])
    warnings = []
    changes = []
    model.warning.connect(warnings.append)
    model.checksChanged.connect(lambda: changes.append(True))

    # The surviving third copy cannot be selected while both others are selected.
    assert not model.setData(model.index(2, 0), Qt.Checked, Qt.CheckStateRole)
    assert warnings
    # Deselecting one candidate makes the previous survivor selectable.
    assert model.setData(model.index(0, 0), Qt.Unchecked, Qt.CheckStateRole)
    assert model.setData(model.index(2, 0), Qt.Checked, Qt.CheckStateRole)
    assert model.selected == {paths[1], paths[2]}
    assert len(changes) == 2


def test_review_category_filter_is_in_memory():
    model = app.ReviewModel(
        [_entry("/tmp/a.jpg", "image"), _entry("/tmp/b.pdf", "document")],
        {"/tmp/a.jpg"}, [["/tmp/a.jpg", "/tmp/b.pdf"]])
    model.set_category("image")
    assert model.rowCount() == 1
    assert model.entry_at(0)["path"] == "/tmp/a.jpg"
    model.set_category("all")
    assert model.rowCount() == 2


def test_group_category_filter_keeps_only_matching_children():
    model = app.GroupModel(lambda _path: (1, 2))
    model.set_groups([
        core.FileGroup(10, "digest", ["/tmp/photo.jpg", "/tmp/notes.pdf"])
    ])
    model.set_category("image")
    parent = model.index(0, 0)
    assert model.rowCount(parent) == 1
    assert model.data(model.index(0, 0, parent), Qt.DisplayRole) == "/tmp/photo.jpg"


def test_similarity_threshold_and_persistent_tag_display():
    low = core.SimPair("/tmp/A", "/tmp/B", 12.0, 10)
    high = core.SimPair("/tmp/C", "/tmp/D", 80.0, 20)
    model = app.SimModel()
    model.set_pairs([low, high])
    model.set_threshold(50)
    assert model.rowCount() == 1
    assert model.pairs[model.root_id(0)] is high
    model.set_tag(high, "До перевірки")
    assert "До перевірки" in model.data(model.index(0, 2), Qt.DisplayRole)
