"""Вибіркове вилучення у «Подібності папок»: чекбокси на обох боках рядка,
інваріант «максимум один бік», Кошик лише з живою копією на другому боці,
чесний shared_total понад кап показу."""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

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


def overlap_scan(tmp_path) -> core.ScanResult:
    make(tmp_path / "t/C/shared.bin", b"S" * 4000)
    make(tmp_path / "t/C/own1.bin", b"1" * 3000)
    make(tmp_path / "t/D/shared.bin", b"S" * 4000)
    make(tmp_path / "t/D/own2.bin", b"2" * 5000)
    return core.scan([str(tmp_path / "t")])


def sim_main(tmp_path):
    r = overlap_scan(tmp_path)
    m = app_mod.Main()
    m.result = r
    m.m_sim.set_pairs(r.sim_pairs)
    return m, r


def test_shared_total_counts_beyond_cap(tmp_path):
    for i in range(65):  # 65 спільних класів > кап 60
        data = bytes([i % 251]) * (100 + i)
        make(tmp_path / "t/A" / f"f{i}.bin", data)
        make(tmp_path / "t/B" / f"f{i}.bin", data)
    make(tmp_path / "t/A/onlyA.bin", b"AAAA" * 999)  # щоб A і B не були 100%-дублями
    make(tmp_path / "t/B/onlyB.bin", b"BBBB" * 777)
    r = core.scan([str(tmp_path / "t")])
    p = r.sim_pairs[0]
    assert len(p.shared) == 60
    assert p.shared_total == 65


def test_truncation_row_shown_not_checkable():
    sp = core.SimPair("/a", "/b", 50.0, 100, [(10, "/a/x.bin", "/b/x.bin")], 5)
    m = app_mod.Main()
    m.m_sim.set_pairs([sp])
    parent = m.m_sim.index(0, 0)
    assert m.m_sim.rowCount(parent) == 2  # 1 рядок + «…і ще»
    extra = m.m_sim.index(1, 0, parent)
    assert "ще 4" in str(m.m_sim.data(extra, Qt.DisplayRole))
    assert not (m.m_sim.flags(extra) & Qt.ItemIsUserCheckable)


def test_check_one_side_per_row(tmp_path):
    m, r = sim_main(tmp_path)
    model = m.m_sim
    parent = model.index(0, 0)
    ia = model.index(0, 0, parent)  # файл у теці A
    ib = model.index(0, 1, parent)  # файл у теці B
    assert model.flags(ia) & Qt.ItemIsUserCheckable
    assert model.setData(ia, 2, Qt.CheckStateRole)
    assert model.data(ia, Qt.CheckStateRole) == Qt.Checked
    assert not model.setData(ib, 2, Qt.CheckStateRole), (
        "другий бік того ж рядка — відмова"
    )
    assert model.data(ib, Qt.CheckStateRole) == Qt.Unchecked
    model.setData(ia, Qt.Unchecked, Qt.CheckStateRole)  # зняли A
    assert model.setData(ib, 2, Qt.CheckStateRole), "після зняття A бік B дозволено"
    model.clear_checks()
    assert model.checked == set()


def test_sim_trash_selected_side(tmp_path, monkeypatch):
    m, r = sim_main(tmp_path)
    _size, fa, fb = r.sim_pairs[0].shared[0]
    parent = m.m_sim.index(0, 0)
    m.m_sim.setData(m.m_sim.index(0, 0, parent), 2, Qt.CheckStateRole)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda ps, **_kwargs: (trashed.extend(ps), [])[1])
    m.delete_checked_sim()
    assert wait_until(lambda: trashed == [fa]), "у Кошик іде САМЕ обраний бік"
    assert wait_until(lambda: fa not in m.result.file_meta), "recompute прибрав файл"
    assert fb in m.result.file_meta


def test_sim_trash_refuses_without_survivor(tmp_path, monkeypatch):
    m, r = sim_main(tmp_path)
    _size, fa, fb = r.sim_pairs[0].shared[0]
    parent = m.m_sim.index(0, 0)
    m.m_sim.setData(m.m_sim.index(0, 0, parent), 2, Qt.CheckStateRole)
    os.remove(fb)  # інша копія зникла з диска — вцілілого нема
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warned.append(str(a[-1]))
    )
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda ps, **_kwargs: (trashed.extend(ps), [])[1])
    m.delete_checked_sim()
    time.sleep(0.2)
    _qapp.processEvents()
    assert trashed == [], "без живої копії на другому боці — НЕ видаляти"
    assert warned, "користувачу пояснено, чому пропущено"
