"""Сортування великих дерев: без proxy/data()-шторму, зі стабільним UX."""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def test_group_sort_uses_cached_values_and_keeps_checked_path():
    calls = 0

    def dates(_path):
        nonlocal calls
        calls += 1
        return 10, 20

    model = app_mod.GroupModel(dates)
    model.set_groups([core.FileGroup(10, "d", ["/z.bin", "/a.bin"])])
    assert calls == 2
    parent = model.index(0, 0)
    model.setData(model.index(0, 0, parent), 2, Qt.CheckStateRole)

    model.sort(0, Qt.AscendingOrder)
    parent = model.index(0, 0)
    assert model.data(model.index(0, 0, parent), Qt.DisplayRole) == "/a.bin"
    assert model.data(model.index(1, 0, parent), Qt.DisplayRole) == "/z.bin"
    assert "/z.bin" in model.checked

    # Повторний render/sort не викликає dates_fn: display і ключі вже готові.
    for _ in range(20):
        model.data(model.index(0, 2, parent), Qt.DisplayRole)
        model.data(model.index(0, 2, parent), Qt.UserRole)
    model.sort(2, Qt.DescendingOrder)
    assert calls == 2


def test_similarity_sort_maps_root_and_children_to_original_pair():
    z = core.SimPair(
        "/z", "/other-z", 70.0, 30,
        [(20, "/z/b.bin", "/other-z/b.bin"),
         (10, "/z/a.bin", "/other-z/a.bin")],
        2,
    )
    a = core.SimPair(
        "/a", "/other-a", 80.0, 5,
        [(5, "/a/x.bin", "/other-a/x.bin")],
        1,
    )
    model = app_mod.SimModel()
    model.set_pairs([z, a])
    model.sort(0, Qt.AscendingOrder)

    first = model.index(0, 0)
    assert model.data(first, Qt.DisplayRole) == "A · /a"
    assert model.data(first.siblingAtColumn(1), Qt.DisplayRole) == "B · /other-a"
    assert model.data(first, Qt.UserRole) == "/a"
    assert model.data(first.siblingAtColumn(1), Qt.UserRole) == "/other-a"
    assert model.path_at(first) == "/a"

    zrow = model.root_row(0)
    zparent = model.index(zrow, 0)
    first_a = model.index(0, 0, zparent)
    first_b = model.index(0, 1, zparent)
    second_a = model.index(1, 0, zparent)
    second_b = model.index(1, 1, zparent)
    assert model.data(first_a, Qt.DisplayRole) == "a.bin"
    assert model.path_at(first_a) == "/z/a.bin"
    assert model.path_at(first_b) == "/other-z/a.bin"
    assert model.path_at(second_a) == "/z/b.bin"
    assert model.path_at(second_b) == "/other-z/b.bin"


def test_views_keep_root_order_and_expanded_group_after_child_sort():
    main = app_mod.Main()
    assert main.v_files.model() is main.m_files
    groups = [
        core.FileGroup(10, "a", ["/a/2", "/a/1"]),
        core.FileGroup(30, "b", ["/b/2", "/b/1"]),
    ]
    main.m_files.set_groups(groups)
    main.show()
    _qapp.processEvents()
    main.v_files.setExpanded(main.m_files.index(0, 0), True)
    parent = main.m_files.index(0, 0)
    main.v_files.setCurrentIndex(main.m_files.index(0, 0, parent))

    main.m_files.sort(0, Qt.AscendingOrder)
    _qapp.processEvents()

    # Групи лишаються у порядку потенційної економії; заголовок сортує
    # копії всередині групи без глобального relayout дерева.
    row = main.m_files.root_row(0)
    assert row == 0
    assert main.v_files.isExpanded(main.m_files.index(row, 0))
    parent = main.m_files.index(row, 0)
    assert main.m_files.data(main.m_files.index(0, 0, parent)) == "/a/1"
    assert main.m_files._path(main.v_files.currentIndex()) == "/a/2"
    main.close()


def test_100k_expanded_tree_sort_stays_interactive_without_filesystem(monkeypatch):
    groups = [
        core.FileGroup(
            4096,
            f"digest-{group}",
            [f"/group-{group}/file-{i:04d}" for i in range(2_000)],
        )
        for group in range(50)
    ]
    main = app_mod.Main()
    main.show()
    main.m_files.set_groups(groups)
    _qapp.processEvents()
    for row in range(len(groups)):
        main.v_files.setExpanded(main.m_files.index(row, 0), True)
    _qapp.processEvents()
    monkeypatch.setattr(
        app_mod.os, "stat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sort must not stat files")))
    monkeypatch.setattr(
        core, "_hash_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sort must not hash files")))

    started = time.perf_counter()
    main.m_files.sort(0, Qt.AscendingOrder)
    _qapp.processEvents()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"сортування 100k шляхів заблокувало UI на {elapsed:.3f} с"
    assert all(
        main.v_files.isExpanded(main.m_files.index(row, 0))
        for row in range(len(groups))
    )
    main.close()


def test_expand_all_yields_to_event_loop():
    groups = [
        core.FileGroup(10, f"d-{i}", [f"/a/{i}", f"/b/{i}"])
        for i in range(200)
    ]
    main = app_mod.Main()
    main.show()
    main.m_files.set_groups(groups)
    _qapp.processEvents()

    started = time.perf_counter()
    main._set_all_expanded(main.v_files, True)
    assert time.perf_counter() - started < 0.1
    assert len(main.m_files._expanded) == 0  # робота ще лише запланована

    deadline = time.monotonic() + 3
    while len(main.m_files._expanded) < len(groups) and time.monotonic() < deadline:
        _qapp.processEvents()
    assert len(main.m_files._expanded) == len(groups)
    main.close()


def test_large_group_is_fetched_in_pages_and_filter_is_model_side():
    paths = [f"/root/file-{i:04d}.bin" for i in range(1200)]
    model = app_mod.GroupModel(lambda _p: (1, 1))
    model.set_groups([core.FileGroup(10, "d", paths)])
    parent = model.index(0, 0)
    assert model.rowCount(parent) == app_mod.PAGE_SIZE + 1
    more = model.index(app_mod.PAGE_SIZE, 0, parent)
    assert "Показати ще" in model.data(more, Qt.DisplayRole)
    assert not model.canFetchMore(parent)  # view must not eagerly drain all pages
    assert model.load_more_at(more)
    assert model.rowCount(parent) == app_mod.PAGE_SIZE * 2 + 1

    model.set_filter("file-1199")
    parent = model.index(0, 0)
    assert model.rowCount(parent) == 1
    assert model.data(model.index(0, 0, parent), Qt.DisplayRole).endswith(
        "file-1199.bin"
    )


def test_similarity_filter_matches_either_directory():
    model = app_mod.SimModel()
    model.set_pairs([
        core.SimPair("/photos/2024", "/backup/a", 50, 10),
        core.SimPair("/music", "/backup/audio", 50, 10),
    ])
    model.set_filter("photos")
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Qt.DisplayRole) == "A · /photos/2024"
    assert model.data(model.index(0, 0), Qt.UserRole) == "/photos/2024"
    model.set_filter("audio")
    assert model.rowCount() == 1
    assert model.data(model.index(0, 1), Qt.DisplayRole) == "B · /backup/audio"
    assert model.data(model.index(0, 1), Qt.UserRole) == "/backup/audio"
    model.set_filter("A ·")
    assert model.rowCount() == 0


def test_column_layout_persists_between_windows(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "settings"))
    first = app_mod.Main()
    header = first.v_files.header()
    header.moveSection(header.visualIndex(2), 0)
    first.v_files.setColumnHidden(3, True)
    header.resizeSection(1, 177)
    first._column_base_widths[first.v_files][1] = 177
    first.m_files.sort(1, Qt.DescendingOrder)
    header.setSortIndicator(1, Qt.DescendingOrder)
    first.close()

    second = app_mod.Main()
    header2 = second.v_files.header()
    assert header2.logicalIndex(0) == 2
    assert second.v_files.isColumnHidden(3)
    assert second._column_base_widths[second.v_files][1] == 177
    assert header2.sortIndicatorSection() == 1
    assert header2.sortIndicatorOrder() == Qt.DescendingOrder
    second.close()
