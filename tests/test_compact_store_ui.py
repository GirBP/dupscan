"""S4 «Solid Core»: кнопка «Прибрати старі сесії (X)» в історії сканувань.

Прибирання — ЛИШЕ за явним кліком і підтвердженням; відкриття діалогу
самé нічого не видаляє.
"""

import json
import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QMessageBox, QPushButton,
)

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.infra.session as session  # noqa: E402

_qapp = QApplication.instance() or QApplication([])

DAY_NS = 24 * 3600 * 10 ** 9


def wait_until(cond, timeout=8.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        _qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _make_session(base_dir: str, created_ns: int, size: int,
                  compressed: bool = True) -> str:
    sdir = os.path.join(base_dir, "sessions")
    os.makedirs(sdir, exist_ok=True)
    suffix = ".json.gz" if compressed else ".json"
    path = os.path.join(sdir, f"{created_ns}{suffix}")
    with open(path, "wb") as fh:
        fh.write(b"x" * size)
    meta = {
        "version": 3, "created_ns": created_ns, "roots": ["/tmp"],
        "partial": False, "files_seen": 1, "bytes_seen": 1,
        "errors_total": 0, "counts": {"files": 0, "dirs": 0, "pairs": 0},
    }
    with open(session._meta_path(path), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)
    return path


def _open_history(main, monkeypatch):
    monkeypatch.setattr(app_mod.QDialog, "exec", lambda _self: None)
    main.show_history()
    dialogs = [d for d in main.findChildren(QDialog)
               if d.windowTitle() == "Історія сканувань"]
    assert dialogs
    return dialogs[-1]


def _compact_button(dlg) -> QPushButton:
    matches = [b for b in dlg.findChildren(QPushButton)
               if b.text().startswith("Прибрати старі")]
    assert matches, "у діалозі історії мусить бути кнопка прибирання"
    return matches[0]


def test_history_dialog_offers_compact_with_size(tmp_path, monkeypatch):
    base = str(tmp_path / "data")
    monkeypatch.setenv("DUPSCAN_DATA_DIR", base)
    now = time.time_ns()
    stale = _make_session(base, now - 45 * DAY_NS, 700, compressed=False)
    fresh = _make_session(base, now - DAY_NS, 700)
    main = app_mod.Main()
    try:
        dlg = _open_history(main, monkeypatch)
        button = _compact_button(dlg)
        assert wait_until(lambda: "(" in button.text()), \
            "підпис мусить показати обсяг прибирання після dry-run"
        assert os.path.exists(stale) and os.path.exists(fresh), \
            "відкриття діалогу нічого не видаляє"

        answers = []

        def fake_question(*_args, **_kwargs):
            answers.append(True)
            return QMessageBox.StandardButton.Yes

        monkeypatch.setattr(
            app_mod.QMessageBox, "question", staticmethod(fake_question))
        button.click()
        assert wait_until(lambda: not os.path.exists(stale)), \
            "після підтвердження старий незжатий legacy зникає"
        assert answers, "прибирання вимагає явного підтвердження"
        assert os.path.exists(fresh), "найновіша сесія недоторканна"
    finally:
        main.close()
        main.deleteLater()
        _qapp.processEvents()


def test_history_dialog_compact_declined_removes_nothing(
        tmp_path, monkeypatch):
    base = str(tmp_path / "data")
    monkeypatch.setenv("DUPSCAN_DATA_DIR", base)
    now = time.time_ns()
    stale = _make_session(base, now - 45 * DAY_NS, 700, compressed=False)
    fresh = _make_session(base, now - DAY_NS, 700)
    main = app_mod.Main()
    try:
        dlg = _open_history(main, monkeypatch)
        button = _compact_button(dlg)
        assert wait_until(lambda: "(" in button.text())
        monkeypatch.setattr(
            app_mod.QMessageBox, "question",
            staticmethod(
                lambda *_a, **_k: QMessageBox.StandardButton.No))
        button.click()
        _qapp.processEvents()
        time.sleep(0.2)
        _qapp.processEvents()
        assert os.path.exists(stale) and os.path.exists(fresh), \
            "відмова у підтвердженні нічого не видаляє"
    finally:
        main.close()
        main.deleteLater()
        _qapp.processEvents()
