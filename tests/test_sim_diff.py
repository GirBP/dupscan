"""Diff-режим пари: рядки «лише в A / лише в B» (включно з унікальними
файлами), ліниво при розгортанні, непозначувані (остання копія). Offscreen."""

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


def make(p, data: bytes):
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


def overlap(tmp_path) -> core.ScanResult:
    make(tmp_path / "t/C/shared.bin", b"S" * 4000)
    make(tmp_path / "t/C/own1.bin", b"1" * 3000)
    make(tmp_path / "t/C/sub/own3.bin", b"3" * 1500)  # унікальний у підтеці
    make(tmp_path / "t/D/shared.bin", b"S" * 4000)
    make(tmp_path / "t/D/own2.bin", b"2" * 5000)
    return core.scan([str(tmp_path / "t")])


def test_pair_diff_core(tmp_path):
    r = overlap(tmp_path)
    pr = r.sim_pairs[0]
    rows_a, rows_b, na, nb = core.pair_diff(r, pr.dir_a, pr.dir_b)
    assert sorted(os.path.basename(p) for _s, p in rows_a) == ["own1.bin", "own3.bin"]
    assert [os.path.basename(p) for _s, p in rows_b] == ["own2.bin"]
    assert (na, nb) == (2, 1)
    # кап поважається, totals — повні
    rows_a1, _rows_b1, na1, _nb1 = core.pair_diff(r, pr.dir_a, pr.dir_b, cap=1)
    assert len(rows_a1) == 1 and na1 == 2


def test_expand_adds_diff_rows_lazily(tmp_path):
    r = overlap(tmp_path)
    m = app_mod.Main()
    m.result = r
    m.m_sim.set_pairs(r.sim_pairs)
    src_parent = m.m_sim.index(0, 0)
    before = m.m_sim.rowCount(src_parent)
    m._sim_expanded(src_parent)
    assert wait_until(lambda: m.m_sim.rowCount(src_parent) > before), (
        "розгортання мусить дорахувати diff-рядки"
    )
    labels = [
        str(m.m_sim.data(m.m_sim.index(i, 2, src_parent), Qt.DisplayRole))
        for i in range(m.m_sim.rowCount(src_parent))
    ]
    assert "спільний" in labels and "лише в A" in labels and "лише в B" in labels
    # повторне розгортання не дублює
    n = m.m_sim.rowCount(src_parent)
    m._sim_expanded(src_parent)
    time.sleep(0.1)
    _qapp.processEvents()
    assert m.m_sim.rowCount(src_parent) == n


def test_diff_rows_not_checkable_but_revealable(tmp_path):
    r = overlap(tmp_path)
    m = app_mod.Main()
    m.result = r
    m.m_sim.set_pairs(r.sim_pairs)
    src_parent = m.m_sim.index(0, 0)
    m._sim_expanded(src_parent)
    assert wait_until(lambda: m.m_sim.rowCount(src_parent) > len(r.sim_pairs[0].shared))
    only_a_row = None
    for i in range(m.m_sim.rowCount(src_parent)):
        if m.m_sim.data(m.m_sim.index(i, 2, src_parent), Qt.DisplayRole) == "лише в A":
            only_a_row = i
            break
    assert only_a_row is not None
    idx0 = m.m_sim.index(only_a_row, 0, src_parent)
    assert not (m.m_sim.flags(idx0) & Qt.ItemIsUserCheckable), (
        "остання копія — непозначувана"
    )
    p = m.m_sim.path_at(idx0)
    assert p and os.path.isabs(p) and os.path.exists(p)
    # чекбокси спільних рядків живі після вставки diff-рядків
    assert m.m_sim.setData(m.m_sim.index(0, 0, src_parent), 2, Qt.CheckStateRole)
