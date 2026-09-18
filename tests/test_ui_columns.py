"""Колонки заповнюють усю ширину: залишок іде «Шляху», вміст не стискається,
resize вікна перерозподіляє. Offscreen-Qt."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def shown_main(tmp_path) -> tuple:
    for top in ("A", "B"):
        make(tmp_path / "t" / top / "x.bin", b"X" * 700)
    r = core.scan([str(tmp_path / "t")])
    m = app_mod.Main()
    m.resize(1100, 700)
    m.show()
    _qapp.processEvents()
    m.m_files.set_groups(r.file_groups)
    return m, m.v_files


def visible_total(view) -> int:
    h = view.header()
    return sum(
        h.sectionSize(c)
        for c in range(view.model().columnCount())
        if not view.isColumnHidden(c)
    )


def test_autofit_fills_viewport(tmp_path):
    m, view = shown_main(tmp_path)
    m._autofit(view)
    assert abs(visible_total(view) - view.viewport().width()) <= 2, (
        "колонки мусять займати всю ширину"
    )
    m.close()


def test_autofit_never_shrinks_path_below_content(tmp_path):
    m, view = shown_main(tmp_path)
    view.resizeColumnToContents(0)
    w0_content = view.header().sectionSize(0)
    m._autofit(view)
    assert view.header().sectionSize(0) >= w0_content
    m.close()


def test_window_resize_redistributes(tmp_path):
    m, view = shown_main(tmp_path)
    m._autofit(view)
    m.resize(900, 700)
    _qapp.processEvents()
    assert abs(visible_total(view) - view.viewport().width()) <= 2, (
        "після resize вікна колонки мусять знову заповнити ширину"
    )
    m.close()


def test_leftover_shared_by_all_columns(tmp_path):
    """Симетрія: вільна ширина дістається КОЖНІЙ колонці, не лише «Шляху»."""
    m, view = shown_main(tmp_path)
    view.resizeColumnToContents(1)
    w1_content = view.header().sectionSize(1)
    m._autofit(view)
    grew = view.header().sectionSize(1) - w1_content
    assert grew > 20, f"колонка даних мусить отримати свою частку (виросла на {grew}px)"
    m.close()


def test_fill_with_hidden_column(tmp_path):
    m, view = shown_main(tmp_path)
    view.setColumnHidden(1, True)
    m._autofit(view)
    assert abs(visible_total(view) - view.viewport().width()) <= 2
    m.close()
