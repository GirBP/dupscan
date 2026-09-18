"""Регресія: злиття тек зі службовими підтеками (__pycache__, .git…).

EXCLUDE_NAMES-теки скан не обходить, тож їхні файли не мають file_meta.
merge_plan свідомо кладе їх у план (категорія uncovered — «усе без
доказу мусить переїхати»), але MergePreparationWorker вимагав метадані
для КОЖНОГО файла плану і падав: «Помилка плану злиття: немає метаданих
файла: …/__pycache__/….pyc» (реальний кейс власника, BARRACUDA).
Правильна поведінка: свіже повне читання і є доказом для таких файлів.
"""

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


def _owner_case(tmp_path):
    """A і B — дублікати; у B службова тека __pycache__ поза сканом."""
    shared = os.urandom(64 * 1024)
    make(tmp_path / "t/A/shared.bin", shared)
    make(tmp_path / "t/B/shared.bin", shared)
    make(tmp_path / "t/B/__pycache__/controls.cpython-311.pyc",
         os.urandom(3000))
    r = core.scan([str(tmp_path / "t")])
    return r, str(tmp_path / "t/B"), str(tmp_path / "t/A")


def _run_preparation(r, src, dst):
    worker = app_mod.MergePreparationWorker(r, src, dst)
    done: list = []
    failures: list[str] = []
    worker.done.connect(done.append)
    worker.failed.connect(failures.append)
    worker.run()  # синхронно, без event loop
    return done, failures


def test_preparation_worker_survives_pycache(tmp_path):
    r, src, dst = _owner_case(tmp_path)
    pyc = os.path.join(src, "__pycache__", "controls.cpython-311.pyc")
    assert pyc not in r.file_meta, "файл службової теки поза сканом"
    done, failures = _run_preparation(r, src, dst)
    assert failures == [], f"підготовка не мусить падати: {failures}"
    assert done and done[0] is not None
    plan, _total, expected_digests, _same, root_identities = done[0]
    assert any(p == pyc for _s, p, _r in plan), "uncovered-файл у плані"
    digest = expected_digests.get(pyc)
    assert isinstance(digest, str) and len(digest) == 64, (
        "свіже повне читання мусить дати digest непокритому файлу")


def test_move_delivers_pycache_then_source_can_be_trashed(
        tmp_path, monkeypatch):
    r, src, dst = _owner_case(tmp_path)
    done, failures = _run_preparation(r, src, dst)
    assert failures == []
    plan, _total, expected_digests, _same, root_identities = done[0]
    moved, errors = app_mod._move_files(
        r, plan, dst, src, expected_digests, root_identities)
    assert errors == [], f"перенос не мусить падати: {errors}"
    assert os.path.exists(
        os.path.join(dst, "__pycache__", "controls.cpython-311.pyc"))
    trashed: list[str] = []
    monkeypatch.setattr(
        app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    kind, _payload = app_mod._verify_then_trash_dir(r, src)
    assert kind == "ok", "після переносу джерело мусить піти в Кошик"
    assert trashed == [src]


def test_copy_accepts_uncovered_with_fresh_digest(tmp_path):
    r, src, dst = _owner_case(tmp_path)
    done, failures = _run_preparation(r, src, dst)
    assert failures == []
    plan, _total, expected_digests, _same, root_identities = done[0]
    copied, errors = app_mod._copy_files(
        r, plan, dst, src, expected_digests, root_identities)
    assert errors == [], f"копіювання не мусить падати: {errors}"
    assert os.path.exists(
        os.path.join(dst, "__pycache__", "controls.cpython-311.pyc"))
    # джерело недоторканне
    assert os.path.exists(
        os.path.join(src, "__pycache__", "controls.cpython-311.pyc"))


def test_raw_copy_without_digests_still_rejects_unproven(tmp_path):
    """Прямий _copy_files БЕЗ карти digest-ів лишається суворим."""
    r, src, dst = _owner_case(tmp_path)
    plan, _total = core.merge_plan(r, src, dst)
    _copied, errors = app_mod._copy_files(r, plan, dst, src)
    assert any("небезпечний план копіювання" in e for e in errors)
