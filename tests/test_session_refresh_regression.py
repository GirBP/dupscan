"""Regression coverage for 2.4 session-refresh lifecycle and relink hints.

The tests deliberately keep filesystem work inside ``tmp_path``.  They use
the same synchronous background adapter only to make UI state transitions
deterministic; production disk checks still run in ``Bg`` workers.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMessageBox,
    QPushButton,
)

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
from dupscan.version import (  # noqa: E402
    DISPLAY_NAME,
    VARIANT_BADGE,
    VARIANT_NAME,
    VERSION,
)

_qapp = QApplication.instance() or QApplication([])


def _sync_bg(fn, on_ok, on_err=None):
    """Run a tiny probe synchronously, retaining the production callback API."""
    try:
        on_ok(fn())
    except Exception as error:  # pragma: no cover - assertion aid
        if on_err is not None:
            on_err(str(error))
        else:
            raise


def _sync_session_load(monkeypatch) -> None:
    monkeypatch.setattr(
        app_mod.SessionLoadWorker, "start", lambda worker: worker.run())


def _historical(root: str) -> core.ScanResult:
    result = core.ScanResult(live=False)
    path = os.path.join(root, "Camera", "one.jpg")
    result.file_meta[path] = core.FileInfo(path, 1, 1, 0, 1, 1, 1)
    return result


def test_commercial_readiness_release_is_unmistakable():
    """The running build identity is visible without relying on Finder."""
    main = app_mod.Main()
    main.show()
    _qapp.processEvents()

    brand = main.findChild(QLabel, "brandTitle")
    variant = main.findChild(QLabel, "versionLabel")
    assert VERSION == "2.26.0"
    assert VERSION in main.windowTitle()
    assert brand is not None and VERSION in brand.text()
    assert variant is not None and VARIANT_NAME in variant.text()
    assert main.release_badge.text() == f"{VERSION} · {VARIANT_BADGE}"
    assert main.release_badge.accessibleName() == DISPLAY_NAME
    for stale in (
            "IDENTITY GUARD", "STORAGE GUARD", "RELEASE GUARD",
            "VISUAL INTEGRITY", "EXFAT PARITY", "TRUE DATES",
            "TRUST BOUNDARIES", "GROUP PROOF", "INSIGHT", "REFERENCE", "ENDURANCE", "CLARITY"):
        assert stale not in main.release_badge.text()
    main.close()


def test_refresh_is_visible_and_enabled_while_large_presentation_prepares(
        monkeypatch, tmp_path):
    """R1: a parsed historical snapshot must not wait for model indexing."""
    main = app_mod.Main()
    main.show()
    root = str(tmp_path / "root")
    historical = _historical(root)
    # Enough records to select the background presentation-index path.
    historical.sim_pairs = [
        core.SimPair(f"/a/{index}", f"/b/{index}", 10.0, 1)
        for index in range(20_001)
    ]
    monkeypatch.setattr(
        app_mod.session, "load_session",
        lambda _path, **_kwargs: historical)
    _sync_session_load(monkeypatch)
    monkeypatch.setattr(main, "_bg_run", _sync_bg)
    monkeypatch.setattr(main, "_ui_bg_run", lambda *_args, **_kwargs: None)

    main._load_session("/large-historical-session.json", [root])
    _qapp.processEvents()

    assert main._model_preparing
    assert main.session_rescan_panel.isVisibleTo(main)
    assert main.b_refresh_session.isVisibleTo(main)
    assert main.b_refresh_session.isEnabled()
    assert main.b_refresh_session.text() == "Інтелектуальний рескан…"
    # Прив'язка до стабільної суті банера, а не до формулювання: текст
    # переписано у 2.19.0, бо старий вимагав повного рескану там, де для
    # пари A/B він не потрібен (див. test_session_pair_merge).
    assert "збережений знімок" in main.session_rescan_text.text()
    main.close()


def test_refresh_cancels_presentation_prepare_before_root_preflight(
        monkeypatch, tmp_path):
    """R1: refresh owns the result transition, not the stale model builder."""
    root = tmp_path / "root"
    root.mkdir()
    main = app_mod.Main()
    main.result = _historical(str(root))
    main._loaded_session_roots = (str(root),)
    preparing = threading.Event()
    main._model_preparing = True
    main._model_prepare_cancel = preparing
    main._model_prepare_token = 41
    preflight = []
    monkeypatch.setattr(main, "_confirm_session_refresh", lambda *_args: True)
    monkeypatch.setattr(
        main, "_preflight_session_refresh",
        lambda *args: preflight.append(args))

    main.refresh_loaded_session()

    assert preparing.is_set()
    assert main._model_prepare_token > 41
    assert preflight and preflight[0][0] is main.result
    assert preflight[0][1] == (str(root),)
    main.close()


def test_visible_rescan_button_dispatches_exactly_one_refresh(
        monkeypatch, tmp_path):
    """SR1: the visible call-to-action must be the real refresh entry point."""
    root = tmp_path / "root"
    root.mkdir()
    main = app_mod.Main()
    main.show()
    historical = _historical(str(root))
    main.result = historical
    main._loaded_session_roots = (str(root),)
    main._finish_model_refresh(historical)
    main._show_task("results")
    _qapp.processEvents()
    starts = []
    monkeypatch.setattr(main, "_confirm_session_refresh", lambda *_args: True)
    monkeypatch.setattr(
        main, "_begin_loaded_session_refresh",
        lambda *args: starts.append(args))

    controls = main.findChildren(
        QPushButton, "refreshLoadedSessionButton")
    assert controls == [main.b_refresh_session]
    assert main.session_rescan_panel.isVisibleTo(main)
    main.b_refresh_session.click()

    assert starts == [(historical, (str(root),))]
    main.close()


def test_verified_relink_hint_resolves_second_loaded_session(
        monkeypatch, tmp_path):
    """R2: active mapping is per-session, verified hint is reusable safely."""
    old_root = tmp_path / "old-volume"
    new_root = tmp_path / "current-volume"
    (new_root / "Camera").mkdir(parents=True)
    (new_root / "Camera" / "one.jpg").write_bytes(b"x")
    main = app_mod.Main()
    main.result = _historical(str(old_root))
    main._loaded_session_roots = (str(old_root),)
    main._last_roots = [str(old_root)]
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(new_root)])

    first = []
    path = str(old_root / "Camera" / "one.jpg")
    main._resolve_session_paths((path,), purpose="Quick Look", on_ready=first.append)
    assert first == [(str(new_root / "Camera" / "one.jpg"),)]
    assert main._session_relink_hints == {str(old_root): str(new_root)}

    # Load another historical snapshot: active mapping clears, hint remains.
    monkeypatch.setattr(
        app_mod.session, "load_session",
        lambda _path, **_kwargs: _historical(str(old_root)))
    _sync_session_load(monkeypatch)
    monkeypatch.setattr(main, "_bg_run", _sync_bg)
    main._load_session("/session-two.json", [str(old_root)])
    assert main._session_root_map == {}
    assert main._session_relink_hints == {str(old_root): str(new_root)}

    # A valid hint must be validated and used without opening the picker again.
    monkeypatch.setattr(
        app_mod, "pick_dirs",
        lambda *_args, **_kwargs: pytest.fail("valid hint must avoid picker"))
    second = []
    main._resolve_session_paths((path,), purpose="Quick Look", on_ready=second.append)
    assert second == [(str(new_root / "Camera" / "one.jpg"),)]
    assert main._session_root_map == {str(old_root): str(new_root)}
    main.close()


def test_symlink_hint_is_rejected_and_cannot_start_pair_verification(
        monkeypatch, tmp_path):
    """R2/R3: stale or symlink hint fails closed before a pair worker exists."""
    old_root = tmp_path / "old-volume"
    real_root = tmp_path / "real-volume"
    (real_root / "A").mkdir(parents=True)
    (real_root / "B").mkdir(parents=True)
    linked_root = tmp_path / "linked-volume"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError as error:  # pragma: no cover - unusual filesystem policy
        pytest.skip(f"symlink unavailable: {error}")
    pair = core.SimPair(str(old_root / "A"), str(old_root / "B"), 100.0, 2)
    main = app_mod.Main()
    main.result = _historical(str(old_root))
    main._loaded_session_roots = (str(old_root),)
    main._last_roots = [str(old_root)]
    main._session_relink_hints = {str(old_root): str(linked_root)}
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(linked_root)])
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *_args: warnings.append(_args))
    starts = []
    monkeypatch.setattr(
        app_mod.PairVerificationWorker, "start", lambda worker: starts.append(worker))

    main._sim_merge(pair, into_a=True)

    assert starts == []
    assert str(old_root) not in main._session_relink_hints
    assert warnings
    main.close()


def test_full_refresh_keeps_existing_declared_root_not_relink_hint(
        monkeypatch, tmp_path):
    """R2: a valid mounted root is authoritative for a full refresh."""
    declared_root = tmp_path / "Volumes" / "L"
    hinted_subfolder = declared_root / "BARRACUDA"
    (declared_root / "Camera").mkdir(parents=True)
    (declared_root / "Camera" / "one.jpg").write_bytes(b"x")
    hinted_subfolder.mkdir()
    main = app_mod.Main()
    historical = _historical(str(declared_root))
    main.result = historical
    main._loaded_session_roots = (str(declared_root),)
    main._session_relink_hints = {str(declared_root): str(hinted_subfolder)}
    main._refresh_preflighting = True
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    starts = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: starts.append(list(worker.roots)))

    main._preflight_session_refresh(
        historical, (str(declared_root),), main._session_context_token)

    assert starts == [[str(declared_root)]]
    assert str(hinted_subfolder) not in starts[0]
    main.refresh_worker = None
    main.close()


def test_full_refresh_ignores_active_pair_relink_when_declared_root_exists(
        monkeypatch, tmp_path):
    """An active A/B relink may not narrow a later full declared-root scan."""
    declared_root = tmp_path / "Volumes" / "L"
    active_subfolder = declared_root / "BARRACUDA"
    (declared_root / "Camera").mkdir(parents=True)
    (declared_root / "Camera" / "one.jpg").write_bytes(b"x")
    active_subfolder.mkdir()
    main = app_mod.Main()
    historical = _historical(str(declared_root))
    main.result = historical
    main._loaded_session_roots = (str(declared_root),)
    # This is the mapping produced by a prior selective pair action, not a
    # persisted hint.  It has exactly the same narrowing risk.
    main._session_root_map = {str(declared_root): str(active_subfolder)}
    main._refresh_preflighting = True
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    starts = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: starts.append(list(worker.roots)))

    main._preflight_session_refresh(
        historical, (str(declared_root),), main._session_context_token)

    assert starts == [[str(declared_root)]]
    assert str(active_subfolder) not in starts[0]
    main.refresh_worker = None
    main.close()


def test_unique_nested_refresh_root_is_detected_without_picker(
        monkeypatch, tmp_path):
    """A uniquely evidenced moved tree must need no generic dialog or picker."""
    declared_root = tmp_path / "Volumes" / "L"
    selected_root = declared_root / "BARRACUDA"
    # The old mount point remains a real directory, but none of the historical
    # relative paths is there any more.
    declared_root.mkdir(parents=True)
    (selected_root / "Camera").mkdir(parents=True)
    (selected_root / "Camera" / "one.jpg").write_bytes(b"x")
    main = app_mod.Main()
    historical = _historical(str(declared_root))
    main.result = historical
    main._loaded_session_roots = (str(declared_root),)
    main._refresh_preflighting = True
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    picker_calls = []
    monkeypatch.setattr(
        app_mod, "pick_dirs",
        lambda *_args, **_kwargs: picker_calls.append(True))
    prompts = []
    monkeypatch.setattr(
        main, "_confirm_manual_refresh_root",
        lambda *_args, **_kwargs: prompts.append(True))
    starts = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: starts.append(list(worker.roots)))

    main._preflight_session_refresh(
        historical, (str(declared_root),), main._session_context_token)

    assert picker_calls == []
    assert prompts == []
    assert starts == [[str(selected_root)]]
    assert str(declared_root) not in starts[0]
    main.refresh_worker = None
    main.close()


def test_load_update_mode_reaches_live_actionable_current_result(
        monkeypatch, tmp_path):
    """Exact user route: old snapshot -> auto root -> live current result."""
    declared_root = tmp_path / "Volumes" / "L"
    current_root = declared_root / "BARRACUDA"
    current_file = current_root / "Camera" / "one.jpg"
    current_file.parent.mkdir(parents=True)
    current_file.write_bytes(b"x")
    historical = _historical(str(declared_root))
    main = app_mod.Main()
    stale_path = str(tmp_path / "previous-session" / "wrong.jpg")
    main.m_files.set_groups([
        core.FileGroup(1, "stale", [stale_path, stale_path + ".copy"])
    ])
    assert main.m_files.groups
    monkeypatch.setattr(
        app_mod.session, "load_session",
        lambda _path, **_kwargs: historical)
    _sync_session_load(monkeypatch)
    monkeypatch.setattr(main, "_bg_run", _sync_bg)
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    monkeypatch.setattr(
        main, "_confirm_manual_refresh_root",
        lambda *_args: pytest.fail("unique auto-root must not prompt"))
    monkeypatch.setattr(
        app_mod, "pick_dirs",
        lambda *_args, **_kwargs: pytest.fail(
            "unique auto-root must not open picker"))
    workers = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: workers.append(worker))
    model_refreshes = []
    monkeypatch.setattr(
        main, "_refresh_models",
        lambda: model_refreshes.append(main.result))

    main._load_session(
        "/historical-session.json", [str(declared_root)],
        refresh_after_load=True)

    assert len(workers) == 1
    worker = workers[0]
    assert worker.roots == [str(current_root)]
    assert worker.session_roots == (str(declared_root),)
    assert worker.root_map == {
        str(declared_root): str(current_root),
    }
    assert worker.source_session_path == "/historical-session.json"
    assert model_refreshes == []
    assert main.m_files.groups == []
    assert "АКТУАЛІЗАЦІЯ" in main.summary.text()

    fresh = core.ScanResult(live=True)
    fresh.file_meta[str(current_file)] = core.FileInfo(
        str(current_file), 1, 1, 0, 1, 1, 1)
    worker.done.emit(fresh)
    _qapp.processEvents()

    assert main.result is fresh
    assert main.result.live
    assert main._last_roots == [str(current_root)]
    assert all(
        path.startswith(str(current_root))
        for path in main.result.file_meta)
    assert model_refreshes == [fresh]
    assert "Дані актуальні" in main._pending_result_note
    assert main._destructive_result_ready()
    main.close()


def test_cancel_during_root_preflight_cannot_start_refresh_worker(
        monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    historical = _historical(str(root))
    main = app_mod.Main()
    main.result = historical
    main._loaded_session_roots = (str(root),)
    queued = []

    def defer(fn, on_ok, on_err=None):
        queued.append((fn, on_ok, on_err))

    monkeypatch.setattr(main, "_ui_bg_run", defer)
    starts = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: starts.append(worker))

    main._begin_loaded_session_refresh(
        historical, (str(root),))
    cancel_event = main._refresh_preflight_cancel
    assert cancel_event is not None
    assert main.b_cancel.isEnabled()

    main.cancel_scan()

    assert cancel_event.is_set()
    assert not main._refresh_preflighting
    assert not main.b_cancel.isEnabled()
    assert "до початку сканування" in main.status.text()

    fn, on_ok, _on_err = queued.pop()
    on_ok(fn())
    assert starts == []
    main.close()


@pytest.mark.parametrize("outcome", ("cancelled", "failed"))
def test_refresh_cancel_or_failure_restores_loaded_historical_rows(
        outcome, monkeypatch, tmp_path):
    declared_root = tmp_path / "Volumes" / "L"
    current_root = declared_root / "BARRACUDA"
    current_file = current_root / "Camera" / "one.jpg"
    current_file.parent.mkdir(parents=True)
    current_file.write_bytes(b"x")
    historical = _historical(str(declared_root))
    historical.file_groups = [
        core.FileGroup(
            1, "historical",
            [
                str(declared_root / "Camera" / "one.jpg"),
                str(declared_root / "Backup" / "one.jpg"),
            ],
        )
    ]
    main = app_mod.Main()
    monkeypatch.setattr(
        app_mod.session, "load_session",
        lambda _path, **_kwargs: historical)
    _sync_session_load(monkeypatch)
    monkeypatch.setattr(main, "_bg_run", _sync_bg)
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    workers = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: workers.append(worker))
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *_args: warnings.append(_args))

    main._load_session(
        "/historical-session.json", [str(declared_root)],
        refresh_after_load=True)
    assert main.m_files.groups == []

    if outcome == "cancelled":
        workers[0].cancelled.emit()
    else:
        workers[0].failed.emit("save failed")
    _qapp.processEvents()

    assert main.result is historical
    assert main.m_files.groups == historical.file_groups
    assert not main._refresh_models_deferred
    assert main.b_refresh_session.isEnabled()
    assert bool(warnings) is (outcome == "failed")
    main.close()


def test_active_pair_relink_never_narrows_full_refresh_without_override(
        monkeypatch, tmp_path):
    """Existing mount + pair mapping requires an explicit refresh decision."""
    declared_root = tmp_path / "Volumes" / "L"
    pair_root = declared_root / "BARRACUDA"
    declared_root.mkdir(parents=True)
    pair_root.mkdir()
    main = app_mod.Main()
    historical = _historical(str(declared_root))
    main.result = historical
    main._loaded_session_roots = (str(declared_root),)
    main._session_root_map = {str(declared_root): str(pair_root)}
    main._refresh_preflighting = True
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    monkeypatch.setattr(
        main, "_confirm_manual_refresh_root",
        lambda *_args, **_kwargs: False)
    starts = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: starts.append(list(worker.roots)))

    main._preflight_session_refresh(
        historical, (str(declared_root),), main._session_context_token)

    assert starts == []
    assert not main._refresh_preflighting
    assert main._session_refresh_root_overrides == {}
    main.close()


def test_stale_scan_done_and_failure_cannot_replace_later_loaded_session(
        monkeypatch, tmp_path):
    """A scan callback captured before session replacement is a no-op."""
    scan_root = tmp_path / "scan-root"
    scan_root.mkdir()
    loaded_root = tmp_path / "loaded-root"
    loaded_root.mkdir()
    main = app_mod.Main()
    monkeypatch.setattr(app_mod.ScanWorker, "start", lambda _worker: None)

    main._begin_scan([str(scan_root)])
    stale_worker = main.worker
    assert stale_worker is not None

    loaded = _historical(str(loaded_root))
    monkeypatch.setattr(
        app_mod.session, "load_session",
        lambda _path, **_kwargs: loaded)
    _sync_session_load(monkeypatch)
    monkeypatch.setattr(main, "_bg_run", _sync_bg)
    main._load_session("/later-session.json", [str(loaded_root)])
    # Make state differences visible if an old callback accidentally runs.
    main.status.setText("стан нової сесії")
    main.b_scan.setEnabled(False)
    main.b_pause.setEnabled(True)
    main.b_cancel.setEnabled(True)
    criticals = []
    monkeypatch.setattr(
        QMessageBox, "critical", lambda *_args: criticals.append(_args))

    stale_worker.done.emit(core.ScanResult(live=True))
    stale_worker.failed.emit("старий scan failure")
    _qapp.processEvents()

    assert main.result is loaded
    assert main.status.text() == "стан нової сесії"
    assert not main.b_scan.isEnabled()
    assert main.b_pause.isEnabled()
    assert main.b_cancel.isEnabled()
    assert criticals == []
    main.close()
