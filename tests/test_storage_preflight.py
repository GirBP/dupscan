"""Storage preflight controls scan startup without blocking or duplication."""

import os
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.infra.storage_guard as storage_guard  # noqa: E402

_qapp = QApplication.instance() or QApplication([])
_GIB = 1024 ** 3


def _wait_until(condition, timeout: float = 5.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        _qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _status(
    level: storage_guard.StorageLevel,
    *,
    free: int | None = None,
    required: int | None = None,
    error: str = "",
) -> storage_guard.StorageStatus:
    return storage_guard.StorageStatus(
        level,
        "/private/data",
        "/private",
        free,
        500 * _GIB if free is not None else None,
        required,
        error,
    )


def _window_with_root(tmp_path):
    window = app.Main()
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    window.folders.add_dir(str(root))
    return window, str(root)


def _run_preflight_inline(window, monkeypatch, status):
    monkeypatch.setattr(
        window,
        "_bg_run",
        lambda fn, on_ok, _on_err=None: on_ok(status),
    )


def test_sufficient_storage_starts_exactly_one_scan(tmp_path, monkeypatch):
    window, root = _window_with_root(tmp_path)
    _run_preflight_inline(
        window,
        monkeypatch,
        _status(storage_guard.StorageLevel.SUFFICIENT, free=20 * _GIB,
                required=10 * _GIB),
    )
    started = []
    monkeypatch.setattr(window, "_begin_scan", started.append)

    window.start_scan()

    assert started == [[root]]
    assert not window._storage_preflighting
    window.close()


def test_low_storage_requires_explicit_continue_or_cancel(tmp_path, monkeypatch):
    window, root = _window_with_root(tmp_path)
    low = _status(
        storage_guard.StorageLevel.LOW,
        free=2 * _GIB,
        required=10 * _GIB,
    )
    _run_preflight_inline(window, monkeypatch, low)
    decisions = iter((False, True))
    seen = []
    started = []
    monkeypatch.setattr(
        window,
        "_confirm_storage_warning",
        lambda status: seen.append(status) or next(decisions),
    )
    monkeypatch.setattr(window, "_begin_scan", started.append)

    window.start_scan()
    assert started == []
    assert "скасовано до читання файлів" in window.status.text()

    window.start_scan()
    assert started == [[root]]
    assert seen == [low, low]
    window.close()


def test_unknown_storage_is_never_treated_as_sufficient(tmp_path, monkeypatch):
    window, root = _window_with_root(tmp_path)
    unknown = _status(
        storage_guard.StorageLevel.UNKNOWN,
        error="volume offline",
    )
    _run_preflight_inline(window, monkeypatch, unknown)
    seen = []
    started = []
    monkeypatch.setattr(
        window,
        "_confirm_storage_warning",
        lambda status: seen.append(status) or True,
    )
    monkeypatch.setattr(window, "_begin_scan", started.append)

    window.start_scan()

    assert seen == [unknown]
    assert started == [[root]]
    window.close()


def test_critical_storage_blocks_before_scan(tmp_path, monkeypatch):
    window, _root = _window_with_root(tmp_path)
    critical = _status(
        storage_guard.StorageLevel.CRITICAL,
        free=256 * 1024 ** 2,
        required=1024 ** 3,
    )
    _run_preflight_inline(window, monkeypatch, critical)
    warnings = []
    started = []
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *args, **_kwargs: warnings.append(args),
    )
    monkeypatch.setattr(window, "_begin_scan", started.append)

    window.start_scan()

    assert started == []
    assert warnings and "268 435 456 байтів" in str(warnings[-1])
    assert "критично мало" in window.status.text()
    window.close()


def test_double_click_does_not_spawn_a_second_preflight_or_scan(
    tmp_path,
    monkeypatch,
):
    window, root = _window_with_root(tmp_path)
    gate = threading.Event()
    probe_calls = []
    started = []
    busy = []

    def probe():
        probe_calls.append(1)
        gate.wait(5)
        return _status(
            storage_guard.StorageLevel.SUFFICIENT,
            free=20 * _GIB,
            required=10 * _GIB,
        )

    monkeypatch.setattr(app.storage_guard, "probe_storage", probe)
    monkeypatch.setattr(window, "_begin_scan", started.append)
    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *args, **_kwargs: busy.append(args),
    )

    window.start_scan()
    window.start_scan()
    assert _wait_until(lambda: len(probe_calls) == 1)
    assert busy

    gate.set()
    assert _wait_until(lambda: started == [[root]])
    assert len(probe_calls) == 1
    window.close()


def test_changed_roots_or_close_suppress_stale_followup(tmp_path, monkeypatch):
    window, _root = _window_with_root(tmp_path)
    gate = threading.Event()
    started = []

    def probe():
        gate.wait(5)
        return _status(
            storage_guard.StorageLevel.SUFFICIENT,
            free=20 * _GIB,
            required=10 * _GIB,
        )

    monkeypatch.setattr(app.storage_guard, "probe_storage", probe)
    monkeypatch.setattr(window, "_begin_scan", started.append)
    window.start_scan()
    window.folders.clear()
    gate.set()
    assert _wait_until(lambda: not window._storage_preflighting)
    assert started == []
    assert "Список джерел змінився" in window.status.text()
    window.close()

    closing, _root = _window_with_root(tmp_path)
    second_gate = threading.Event()
    second_started = []

    def second_probe():
        second_gate.wait(5)
        return _status(
            storage_guard.StorageLevel.SUFFICIENT,
            free=20 * _GIB,
            required=10 * _GIB,
        )

    monkeypatch.setattr(
        app.storage_guard,
        "probe_storage",
        second_probe,
    )
    monkeypatch.setattr(closing, "_begin_scan", second_started.append)
    closing.show()
    closing.start_scan()
    closing.close()
    second_gate.set()
    assert _wait_until(lambda: not closing.isVisible())
    assert second_started == []
