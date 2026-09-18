"""Historical sessions can explicitly relink moved roots without a full scan."""

import os
import sys
import tempfile
import time
from copy import deepcopy

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _wait_until(condition, timeout=10.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        _qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _make(path, data: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _loaded_main(old_root) -> app_mod.Main:
    main = app_mod.Main()
    main.result = core.ScanResult(live=False)
    main._loaded_session_roots = (str(old_root),)
    main._last_roots = [str(old_root)]
    return main


def test_rebase_uses_longest_containing_root_without_disk_io():
    roots = ("/Volumes/L", "/Volumes/L/archive")
    path = "/Volumes/L/archive/A/file.jpg"

    root = app_mod.containing_session_root(path, roots)

    assert root == "/Volumes/L/archive"
    assert app_mod.rebase_session_path(
        path, root, "/Volumes/New") == "/Volumes/New/A/file.jpg"
    with pytest.raises(ValueError):
        app_mod.rebase_session_path("/outside/file.jpg", root, "/Volumes/New")


def test_live_result_invalidates_session_mapping(tmp_path):
    old_root = tmp_path / "old"
    main = _loaded_main(old_root)
    main._session_root_map[str(old_root)] = str(tmp_path / "new")
    token = main._session_context_token

    main.on_done(core.ScanResult(live=True))

    assert main._loaded_session_roots == ()
    assert main._session_root_map == {}
    assert main._session_context_token > token
    main.close()


def test_historical_session_sweep_never_prunes_moved_paths(
        tmp_path, monkeypatch):
    old_root = tmp_path / "old"
    main = _loaded_main(old_root)
    main.result.sim_pairs = [
        core.SimPair(str(old_root / "A"), str(old_root / "B"), 80.0, 10)
    ]
    probes = []
    recomputes = []
    monkeypatch.setattr(
        app_mod.os.path, "exists",
        lambda path: (probes.append(path), False)[1])
    monkeypatch.setattr(
        main, "_recompute_async",
        lambda missing, note: recomputes.append((missing, note)))

    main._on_app_state(Qt.ApplicationActive)
    _qapp.processEvents()

    assert probes == []
    assert recomputes == []
    assert len(main.result.sim_pairs) == 1
    main.close()


def test_missing_loaded_path_can_relink_and_open_current_file(
        tmp_path, monkeypatch):
    old_root = tmp_path / "old-volume"
    new_root = tmp_path / "current-volume"
    old_file = old_root / "Pictures" / "photo.jpg"
    current_file = new_root / "Pictures" / "photo.jpg"
    _make(current_file)
    main = _loaded_main(old_root)
    historical = deepcopy(main.result)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(new_root)])
    opened = []

    main._open_result_path(str(old_file), opened.append)

    assert _wait_until(lambda: opened == [str(current_file)])
    assert main._session_root_map == {str(old_root): str(new_root)}
    assert main.result == historical
    main.close()


def test_wrong_relink_target_is_rejected_with_both_paths(
        tmp_path, monkeypatch):
    old_root = tmp_path / "old-volume"
    wrong_root = tmp_path / "wrong-volume"
    wrong_root.mkdir()
    old_file = old_root / "Pictures" / "photo.jpg"
    main = _loaded_main(old_root)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(
        app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(wrong_root)])
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, message: warnings.append((title, message)))
    opened = []

    main._open_result_path(str(old_file), opened.append)

    assert _wait_until(lambda: bool(warnings))
    assert opened == []
    assert main._session_root_map == {}
    assert str(old_file) in warnings[0][1]
    assert str(wrong_root / "Pictures" / "photo.jpg") in warnings[0][1]
    main.close()


def test_cancelled_relink_does_not_launch_or_change_mapping(
        tmp_path, monkeypatch):
    old_root = tmp_path / "old-volume"
    old_file = old_root / "missing.bin"
    main = _loaded_main(old_root)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No)
    opened = []

    main._open_result_path(str(old_file), opened.append)

    assert _wait_until(lambda: "скасовано" in main.status.text().casefold())
    assert opened == []
    assert main._session_root_map == {}
    main.close()


def test_opener_failure_becomes_visible_warning(tmp_path, monkeypatch):
    path = tmp_path / "exists.bin"
    _make(path)
    main = app_mod.Main()
    main.result = core.ScanResult(live=True)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, message: warnings.append((title, message)))

    def fail(_path):
        raise OSError("LaunchServices unavailable")

    main._open_result_path(str(path), fail)

    assert _wait_until(lambda: bool(warnings))
    assert "LaunchServices unavailable" in warnings[0][1]
    assert str(path) in warnings[0][1]
    main.close()


def test_moved_loaded_pair_scans_only_translated_a_and_b(
        tmp_path, monkeypatch):
    old_root = tmp_path / "old-volume"
    new_root = tmp_path / "current-volume"
    current_a = new_root / "A"
    current_b = new_root / "B"
    _make(current_a / "shared.bin", b"shared" * 200)
    _make(current_b / "shared-copy.bin", b"shared" * 200)
    _make(new_root / "unrelated" / "large-placeholder.bin", b"unrelated")
    pair = core.SimPair(
        str(old_root / "A"), str(old_root / "B"), 100.0, 1_200)
    main = _loaded_main(old_root)
    historical = deepcopy(main.result)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(new_root)])
    real_scan = core.scan
    scans = []

    def traced_scan(roots, *args, **kwargs):
        scans.append(tuple(roots))
        return real_scan(roots, *args, **kwargs)

    monkeypatch.setattr(app_mod.core, "scan", traced_scan)
    merged = []
    monkeypatch.setattr(
        main, "_run_sim_merge",
        lambda current, into_a, fresh, **kwargs:
        merged.append((current, into_a, fresh, kwargs)))

    main._sim_merge(pair, into_a=True)

    assert _wait_until(lambda: bool(merged))
    assert scans == [(str(current_a), str(current_b))]
    assert str(new_root) not in scans[0]
    assert str(old_root) not in scans[0]
    current, into_a, fresh, kwargs = merged[0]
    assert (current.dir_a, current.dir_b) == (str(current_a), str(current_b))
    assert into_a is True
    assert fresh.live and not fresh.partial
    assert kwargs["pair_verified"] is True
    assert (pair.dir_a, pair.dir_b) == (
        str(old_root / "A"), str(old_root / "B"))
    assert main.result == historical
    main.close()


def test_loaded_remove_shared_uses_fresh_translated_pair(
        tmp_path, monkeypatch):
    old_root = tmp_path / "old-volume"
    new_root = tmp_path / "current-volume"
    current_a = new_root / "A"
    current_b = new_root / "B"
    _make(current_a / "shared.bin", b"same" * 300)
    _make(current_b / "shared-copy.bin", b"same" * 300)
    pair = core.SimPair(
        str(old_root / "A"), str(old_root / "B"), 100.0, 1_200)
    main = _loaded_main(old_root)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(new_root)])
    calls = []

    def capture(compute_fn, **kwargs):
        calls.append((compute_fn(), kwargs))

    monkeypatch.setattr(main, "_sim_trash_flow", capture)

    main._sim_remove_shared(pair, remove_from_a=True)

    assert _wait_until(lambda: bool(calls))
    paths, kwargs = calls[0]
    assert paths == [str(current_a / "shared.bin")]
    assert kwargs["verified_result"].live
    assert kwargs["historical_snapshot"] is True
    main.close()


def test_loaded_pair_menu_explains_selective_verification(tmp_path, monkeypatch):
    pair = core.SimPair(
        str(tmp_path / "old" / "A"), str(tmp_path / "old" / "B"), 50.0, 10)
    main = _loaded_main(tmp_path / "old")
    main.m_sim.set_pairs([pair])
    root = main.m_sim.index(0, 0)
    monkeypatch.setattr(main.v_sim, "indexAt", lambda _pos: root)

    menu = main._sim_menu(QPoint(0, 0), execute=False)
    labels = {action.text() for action in menu.actions()}

    assert "Перевірити й прибрати спільне з A → Кошик" in labels
    assert "Перевірити й прибрати спільне з B → Кошик" in labels
    assert "Перевірити й злити B → A…" in labels
    assert "Перевірити й злити A → B…" in labels
    main.close()
