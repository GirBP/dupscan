"""Злиття пари: перенос унікального з одного боку в інший (той самий том,
колізії → « (2)»), потім тека-джерело в Кошик ЛИШЕ після перевірки, що все
лишкове має живу копію поза нею. Offscreen-Qt."""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def overlap(tmp_path) -> core.ScanResult:
    make(tmp_path / "t/C/shared.bin", b"S" * 4000)
    make(tmp_path / "t/C/own1.bin", b"1" * 3000)
    make(tmp_path / "t/D/shared.bin", b"S" * 4000)
    make(tmp_path / "t/D/own2.bin", b"2" * 5000)
    make(tmp_path / "t/D/sub/own3.bin", b"3" * 1500)  # унікальний у підтеці
    return core.scan([str(tmp_path / "t")])


def merge_main(tmp_path, answers, monkeypatch):
    r = overlap(tmp_path)
    m = app_mod.Main()
    m.result = r
    m.m_sim.set_pairs(r.sim_pairs)
    it = iter(answers)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: next(it))
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda ps, **_kwargs: (trashed.extend(ps), [])[1])
    pr = next(
        p
        for p in r.sim_pairs
        if os.path.basename(p.dir_a) == "C" and os.path.basename(p.dir_b) == "D"
    )
    return m, r, pr, trashed


def test_merge_plan_full_unique(tmp_path):
    r = overlap(tmp_path)
    pr = next(p for p in r.sim_pairs if os.path.basename(p.dir_a) == "C")
    plan, total = core.merge_plan(r, pr.dir_b, pr.dir_a)  # D -> C
    rels = sorted(rel for _s, _p, rel in plan)
    assert rels == ["own2.bin", os.path.join("sub", "own3.bin")]
    assert total == 5000 + 1500


def test_merge_moves_unique_then_trashes_src(tmp_path, monkeypatch):
    Yes = QMessageBox.StandardButton.Yes
    m, r, pr, trashed = merge_main(tmp_path, [Yes, Yes], monkeypatch)
    m._sim_merge(pr, into_a=True)  # злити D в C
    assert wait_until(lambda: trashed == [pr.dir_b]), "тека D мусить піти в Кошик"
    assert os.path.exists(os.path.join(pr.dir_a, "own2.bin"))
    assert os.path.exists(os.path.join(pr.dir_a, "sub", "own3.bin"))
    assert not os.path.exists(os.path.join(pr.dir_b, "own2.bin"))
    assert wait_until(lambda: pr.dir_b not in m.result.dir_ok), "recompute прибрав D"


def test_merge_collision_renames(tmp_path, monkeypatch):
    make(tmp_path / "t/C/own2.bin", b"INNA" * 100)  # колізія імені, інший вміст
    Yes = QMessageBox.StandardButton.Yes
    m, r, pr, trashed = merge_main(tmp_path, [Yes, Yes], monkeypatch)
    m._sim_merge(pr, into_a=True)
    assert wait_until(lambda: trashed == [pr.dir_b])
    assert os.path.exists(os.path.join(pr.dir_a, "own2 (2).bin")), (
        "колізія мусить дати « (2)», не перезапис"
    )
    assert os.path.exists(os.path.join(pr.dir_a, "own2.bin"))  # старий цілий


def test_merge_cross_device_refused(tmp_path, monkeypatch):
    Yes = QMessageBox.StandardButton.Yes
    m, r, pr, trashed = merge_main(tmp_path, [Yes, Yes], monkeypatch)
    infos: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda *a, **k: infos.append(str(a[-1]))
    )
    monkeypatch.setattr(app_mod, "_same_device", lambda a, b: False)
    m._sim_merge(pr, into_a=True)
    time.sleep(0.1)
    _qapp.processEvents()
    assert infos and "том" in infos[0]
    assert trashed == []
    assert os.path.exists(os.path.join(pr.dir_b, "own2.bin"))  # нічого не рушило


def test_merge_declined_trash_keeps_dir(tmp_path, monkeypatch):
    Yes, No = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
    m, r, pr, trashed = merge_main(tmp_path, [Yes, No], monkeypatch)
    m._sim_merge(pr, into_a=True)
    assert wait_until(lambda: os.path.exists(os.path.join(pr.dir_a, "own2.bin")))
    time.sleep(0.2)
    _qapp.processEvents()
    assert trashed == []  # відмова 2-го кроку: перенесено, але теку не чіпаємо
    assert os.path.isdir(pr.dir_b)


def test_merge_aborts_trash_without_survivor(tmp_path, monkeypatch):
    Yes = QMessageBox.StandardButton.Yes
    m, r, pr, trashed = merge_main(tmp_path, [Yes, Yes], monkeypatch)
    warned: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *a, **k: warned.append(str(a[-1]))
    )
    c_shared = os.path.join(pr.dir_a, "shared.bin")
    os.remove(c_shared)  # реальна копія зникла; exists() mock уже недостатній
    m._sim_merge(pr, into_a=True)
    assert wait_until(lambda: bool(warned)), "мусить пояснити відмову"
    assert trashed == []  # без живої копії shared поза D — теку НЕ чіпати
    assert os.path.isdir(pr.dir_b)
