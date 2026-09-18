"""Opt-in proof against the user's real historical L-volume session.

This test parses the saved snapshot and runs only the bounded root preflight.
It intercepts ``SessionRefreshWorker.start`` so no disk scan is performed.
Run explicitly with ``DUPSCAN_REAL_SESSION_TEST=1``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402

_qapp = QApplication.instance() or QApplication([])
_SESSION_PATH = (
    Path.home()
    / "Library/Application Support/DupScan/sessions/1784676907489740000.json"
)
_OLD_ROOT = "/Volumes/L"
_CURRENT_ROOT = "/Volumes/L/BARRACUDA"


def _sync_bg(fn, on_ok, on_err=None):
    try:
        on_ok(fn())
    except Exception as error:  # pragma: no cover - assertion aid
        if on_err is not None:
            on_err(str(error))
        else:
            raise


@pytest.mark.skipif(
    os.environ.get("DUPSCAN_REAL_SESSION_TEST") != "1"
    or not _SESSION_PATH.is_file()
    or not Path(_CURRENT_ROOT).is_dir(),
    reason="explicit local real-session proof only",
)
def test_real_l_session_load_update_selects_barracuda_without_full_scan(
        monkeypatch):
    main = app_mod.Main()
    monkeypatch.setattr(
        app_mod.SessionLoadWorker, "start", lambda worker: worker.run())
    monkeypatch.setattr(main, "_bg_run", _sync_bg)
    monkeypatch.setattr(main, "_ui_bg_run", _sync_bg)
    monkeypatch.setattr(
        main, "_confirm_manual_refresh_root",
        lambda *_args: pytest.fail("real session must auto-detect root"))
    monkeypatch.setattr(
        app_mod, "pick_dirs",
        lambda *_args, **_kwargs: pytest.fail(
            "real session must not open root picker"))
    monkeypatch.setattr(
        main, "_refresh_models",
        lambda: pytest.fail(
            "historical model must not build before live refresh"))
    workers = []
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: workers.append(worker))

    main._load_session(
        str(_SESSION_PATH), [_OLD_ROOT], refresh_after_load=True)

    assert main.result is not None
    assert main.result.files_seen == 383_206
    assert len(workers) == 1
    assert workers[0].roots == [_CURRENT_ROOT]
    assert workers[0].session_roots == (_OLD_ROOT,)
    assert workers[0].root_map == {_OLD_ROOT: _CURRENT_ROOT}
    main.refresh_worker = None
    main.close()
