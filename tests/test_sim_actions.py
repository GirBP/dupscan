"""Дії рівня пари у «Подібності папок»: масове «Прибрати спільне з одного
боку» (повний список, фонова верифікація) та «Ігнорувати пару»
(персистентно в сесії). Offscreen-Qt."""

import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402

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


def rich_overlap(tmp_path) -> core.ScanResult:
    """C і D: 2 спільні класи (один — у підтеках із РІЗНИМИ іменами/шляхами),
    по одному власному файлу з кожного боку."""
    make(tmp_path / "t/C/shared.bin", b"S" * 4000)
    make(tmp_path / "t/C/sub/deep1.bin", b"Q" * 2500)
    make(tmp_path / "t/C/own1.bin", b"1" * 3000)
    make(tmp_path / "t/D/shared.bin", b"S" * 4000)
    make(tmp_path / "t/D/other/renamed.bin", b"Q" * 2500)  # той самий вміст, інший шлях
    make(tmp_path / "t/D/own2.bin", b"2" * 5000)
    return core.scan([str(tmp_path / "t")])


def sim_main(tmp_path):
    r = rich_overlap(tmp_path)
    m = app_mod.Main()
    m.result = r
    m.m_sim.set_pairs(r.sim_pairs)
    return m, r, r.sim_pairs[0]


def test_shared_side_full_and_position_independent(tmp_path):
    r = rich_overlap(tmp_path)
    pr = r.sim_pairs[0]
    a_side = core.shared_side(r, pr.dir_a, pr.dir_b, remove_from_a=True)
    b_side = core.shared_side(r, pr.dir_a, pr.dir_b, remove_from_a=False)
    assert sorted(os.path.basename(p) for p in a_side) == ["deep1.bin", "shared.bin"]
    assert sorted(os.path.basename(p) for p in b_side) == ["renamed.bin", "shared.bin"]
    assert all(p.startswith(pr.dir_a + os.sep) for p in a_side)
    assert all(p.startswith(pr.dir_b + os.sep) for p in b_side)


def test_bulk_remove_shared_from_side_a(tmp_path, monkeypatch):
    m, r, pr = sim_main(tmp_path)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda ps, **_kwargs: (trashed.extend(ps), [])[1])
    m._sim_remove_shared(pr, remove_from_a=True)
    assert wait_until(
        lambda: (
            sorted(os.path.basename(p) for p in trashed) == ["deep1.bin", "shared.bin"]
        )
    )
    assert wait_until(
        lambda: all(
            "own" in os.path.basename(p) or p not in trashed for p in r.file_meta
        )
    )
    # власні файли обох боків живі у стані
    assert any(p.endswith("own1.bin") for p in m.result.file_meta)
    assert any(p.endswith("own2.bin") for p in m.result.file_meta)


def test_bulk_remove_skips_when_other_copy_gone(tmp_path, monkeypatch):
    m, r, pr = sim_main(tmp_path)
    gone = next(p for p in r.file_meta if p.endswith("renamed.bin"))
    os.remove(gone)  # B-копія deep1-класу зникла -> A-файл без вцілілого
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
    m._sim_remove_shared(pr, remove_from_a=True)
    assert wait_until(lambda: trashed and warned)
    assert sorted(os.path.basename(p) for p in trashed) == ["shared.bin"]


def test_bulk_verify_does_not_block_gui(tmp_path, monkeypatch):
    m, r, pr = sim_main(tmp_path)
    gate = threading.Event()
    real = os.lstat

    def slow_lstat(p, *a, **k):
        gate.wait(5)
        return real(p, *a, **k)

    monkeypatch.setattr(app_mod.os, "lstat", slow_lstat)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
    )
    t0 = time.monotonic()
    m._sim_remove_shared(pr, remove_from_a=True)
    assert time.monotonic() - t0 < 0.3, "верифікація мусить іти у фоні"
    gate.set()
    assert wait_until(lambda: not m._trashing)


def test_ignore_pair_persists_via_session(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    m, r, pr = sim_main(tmp_path)
    key = (pr.dir_a, pr.dir_b)
    m._sim_ignore(pr)
    assert key in r.ignored_pairs
    # миттєво зникла з видачі (пара підтек sub~other лишається — вона інша)
    assert all((p.dir_a, p.dir_b) != key for p in m.m_sim.pairs)
    # агрегація поважає ігнор
    core._aggregate(r)
    assert all((p.dir_a, p.dir_b) != key for p in r.sim_pairs)
    # переживає save/load
    p = session.save_session(r, [os.path.commonpath((pr.dir_a, pr.dir_b))])
    loaded = session.load_session(p)
    assert key in loaded.ignored_pairs
    assert all((sp.dir_a, sp.dir_b) != key for sp in loaded.sim_pairs)
    # скидання повертає пару
    m.result = loaded
    m._sim_reset_ignored()
    assert wait_until(
        lambda: any((sp.dir_a, sp.dir_b) == key for sp in m.result.sim_pairs)
    )
