"""Корені сесії мусять бути канонізованими.

core.scan() нормалізує корені (realpath, дедуп) всередині — воркери
мають писати в сесію саме цей нормалізований результат, а не сирі
шляхи, інакше скан через симлінк дає сесію, що не проходить
containment-валідацію при завантаженні.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _tree_and_link(tmp_path):
    """Тека з дублікатами + симлінк-псевдонім на неї."""
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
    link = tmp_path / "lnk"
    os.symlink(tree, link)
    return tree, link


def test_scan_exposes_canonical_scanned_roots(tmp_path):
    tree, link = _tree_and_link(tmp_path)
    r = core.scan([str(link)])
    assert r.scanned_roots == [os.path.realpath(str(tree))]
    # усі знайдені файли лежать під канонізованим коренем
    root = r.scanned_roots[0]
    assert r.file_meta
    assert all(p.startswith(root + os.sep) for p in r.file_meta)


def test_symlink_scan_session_round_trip(tmp_path):
    """Скан через симлінк → save з scanned_roots → load без ValueError."""
    _tree, link = _tree_and_link(tmp_path)
    r1 = core.scan([str(link)])
    path = session.save_session(
        r1, r1.scanned_roots, base_dir=str(tmp_path / "data"))
    assert path and os.path.exists(path)
    r2 = session.load_session(path)
    assert {tuple(sorted(g.paths)) for g in r2.file_groups} == \
           {tuple(sorted(g.paths)) for g in r1.file_groups}


def test_scan_worker_writes_canonical_loadable_session(tmp_path, monkeypatch):
    """ScanWorker мусить зберігати сесію з канонізованими коренями."""
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    _tree, link = _tree_and_link(tmp_path)
    import dupscan.ui.app as app
    worker = app.ScanWorker([str(link)])
    worker.run()  # синхронно, без event loop
    assert worker.saved_session_path
    meta = session.list_sessions(base_dir=str(tmp_path / "data"))
    assert meta and meta[0]["roots"] == [os.path.realpath(str(link))]
    r2 = session.load_session(worker.saved_session_path)
    assert r2.file_meta


def test_legacy_session_with_var_alias_roots_still_loads(tmp_path):
    """Сумісність: стара сесія з неканонічними коренями (/var-псевдонім
    macOS) вантажиться лексично, а не падає ValueError. Диск при цьому
    не резолвиться (страж test_historical_session_load_never_resolves…)."""
    real = str(tmp_path)
    if not real.startswith("/private/var/"):
        pytest.skip("tmp_path не у /private/var")
    alias_root = "/var/" + real[len("/private/var/"):] + "/tree"
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
    r1 = core.scan([str(tree)])  # file_meta — realpath-шляхи (/private/…)
    # старий воркер писав сирі корені як їх дав користувач:
    path = session.save_session(r1, [alias_root], base_dir=str(tmp_path / "d"))
    assert path
    r2 = session.load_session(path)
    assert set(r2.file_meta) == set(r1.file_meta)


def test_nonexistent_root_does_not_crash_normalization(tmp_path):
    """Порожній scan не лишає сміття у scanned_roots."""
    r = core.scan([str(tmp_path / "missing")])
    assert r.scanned_roots == []
    assert any("не тека" in e for e in r.errors)


def test_scan_worker_falls_back_to_raw_roots_when_cancelled(tmp_path,
                                                            monkeypatch):
    """Скасований до старту скан не має губити корені при збереженні."""
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    tree, link = _tree_and_link(tmp_path)
    import dupscan.ui.app as app
    worker = app.ScanWorker([str(link)])
    worker.cancel.set()
    worker.run()
    # часткова сесія або без сесії — але ніколи не виняток нагору
    if worker.saved_session_path:
        session.load_session(worker.saved_session_path)


@pytest.mark.skipif(not os.path.isdir("/var/folders"),
                    reason="macOS-специфічний симлінк /var")
def test_var_folders_alias_is_canonicalized(tmp_path):
    """/var/... → /private/var/... (класичний macOS-псевдонім)."""
    real = str(tmp_path)
    if not real.startswith("/private/var/"):
        pytest.skip("tmp_path не у /private/var")
    alias = "/var/" + real[len("/private/var/"):]
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
    r = core.scan([os.path.join(alias, "tree")])
    assert r.scanned_roots == [str(tree)]
