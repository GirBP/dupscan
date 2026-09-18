import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

import dupscan.ui.app as app
import dupscan.domain.core as core


_qapp = QApplication.instance() or QApplication([])


def model(groups):
    result = app.GroupModel(lambda path: (0, {p: i for i, p in enumerate(
        path for group in groups for path in group.paths
    )}[path]))
    result.set_groups(groups)
    return result


def test_select_group_safe_keeps_configured_survivor():
    groups = [core.FileGroup(10, "a", ["/a/old", "/a/keep", "/a/new"])]
    m = model(groups)
    m.keeper_fn = lambda paths: "/a/keep"

    m.select_group_safe(0)

    assert m.checked == {"/a/old", "/a/new"}
    assert m.selection_stats() == {"count": 2, "bytes": 20, "groups": 1}


def test_filtered_selection_includes_unloaded_paths_and_preserves_hidden_selection():
    paths = [f"/a/match-{n:03d}" for n in range(app.PAGE_SIZE + 5)]
    paths += ["/a/hidden"]
    m = model([core.FileGroup(4, "a", paths)])
    m.set_filter("match-")
    m.checked.add("/a/hidden")

    m.select_filtered_safe()

    assert "/a/hidden" in m.checked
    assert sum(path.startswith("/a/match-") for path in m.checked) == len(paths) - 2
    assert len(m.checked) == len(paths) - 1


def test_clear_filtered_does_not_clear_hidden_selection_and_stats_accept_paths():
    groups = [
        core.FileGroup(10, "a", ["/a/match", "/a/hidden"]),
        core.FileGroup(5, "b", ["/b/match", "/b/other"]),
    ]
    m = model(groups)
    m.checked = {"/a/hidden", "/a/match", "/b/match"}
    m.set_filter("match")

    assert m.selection_stats(["/a/hidden", "/b/match", "/missing"]) == {
        "count": 2, "bytes": 15, "groups": 2,
    }
    m.clear_filtered()

    assert m.checked == {"/a/hidden"}


def test_select_all_safe_keeps_one_per_group_and_legacy_alias_is_safe():
    groups = [
        core.FileGroup(10, "a", ["/a/1", "/a/2"]),
        core.FileGroup(20, "b", ["/b/1", "/b/2", "/b/3"]),
    ]
    m = model(groups)
    m.keeper_fn = lambda paths: paths[0]

    m.select_all_safe()
    assert m.checked == {"/a/2", "/b/2", "/b/3"}
    m.clear_checks()
    m.select_all_dups()
    assert m.checked == {"/a/2", "/b/2", "/b/3"}
