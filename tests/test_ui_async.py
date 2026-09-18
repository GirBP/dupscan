"""GUI-потік ніколи не блокується диском: сесія зберігається у скан-потоці,
sweep зниклих шляхів і Кошик — фонові. Offscreen-Qt."""

import errno
import os
import sys
import tempfile
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFileDialog, QLineEdit, QMessageBox,
)

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


def test_scan_worker_saves_session_and_dates(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    for top in ("A", "B"):
        make(tmp_path / "tree" / top / "x.bin", b"X" * 500)
    w = app_mod.ScanWorker([str(tmp_path / "tree")])
    got = []
    w.done.connect(got.append)
    w.run()  # синхронно в цьому потоці — нам потрібен лише побічний ефект
    assert got and got[0].file_groups
    sdir = tmp_path / "data" / "sessions"
    payloads = [
        n
        for n in os.listdir(sdir)
        if app_mod.session._is_payload(n)
    ]
    assert len(payloads) == 1, "сесію зберігає СКАН-потік, до сигналу done"
    assert isinstance(w.dir_dates, dict)


def test_scan_session_save_failure_is_explicit_in_results(
    tmp_path,
    monkeypatch,
):
    for top in ("A", "B"):
        make(tmp_path / "tree" / top / "x.bin", b"X" * 500)
    monkeypatch.setattr(app_mod.session, "save_session", lambda *_a, **_k: "")
    worker = app_mod.ScanWorker([str(tmp_path / "tree")])
    results = []
    worker.done.connect(results.append)
    worker.run()
    assert results and worker.saved_session_path == ""

    window = app_mod.Main()
    window.worker = worker
    window.on_done(results[0])
    assert "історію цієї перевірки не збережено" in window.status.text()
    window.close()


def test_on_done_never_touches_disk(tmp_path, monkeypatch):
    make(tmp_path / "t" / "a.bin", b"A" * 100)
    r = core.scan([str(tmp_path / "t")])
    m = app_mod.Main()
    called = []
    monkeypatch.setattr(
        app_mod.session, "save_session", lambda *a, **k: called.append(1)
    )
    m.on_done(r)
    assert not called, "on_done не має сам зберігати сесію (це робота воркера)"


def test_app_state_sweep_does_not_block_gui(tmp_path, monkeypatch):
    for top in ("A", "B"):
        make(tmp_path / "t" / top / "x.bin", b"X" * 700)
    r = core.scan([str(tmp_path / "t")])
    m = app_mod.Main()
    m.result = r
    gate = threading.Event()

    def slow_exists(p):
        gate.wait(5)
        return False  # усе «зникло»

    monkeypatch.setattr(app_mod.os.path, "exists", slow_exists)
    seen = []
    monkeypatch.setattr(
        m, "_recompute_async", lambda missing, note: seen.append(missing)
    )
    t0 = time.monotonic()
    m._on_app_state(Qt.ApplicationActive)
    assert time.monotonic() - t0 < 0.3, "sweep мусить піти у фон, не блокуючи GUI"
    gate.set()
    assert wait_until(lambda: seen), "фоновий sweep мусить донести зниклі шляхи"
    assert seen[0]  # missing непорожній


def test_sweep_throttled_and_not_overlapping(tmp_path, monkeypatch):
    for top in ("A", "B"):  # мусять бути групи, інакше sweep-у нічого перевіряти
        make(tmp_path / "t" / top / "x.bin", b"X" * 700)
    r = core.scan([str(tmp_path / "t")])
    m = app_mod.Main()
    m.result = r
    calls = []
    monkeypatch.setattr(app_mod.os.path, "exists", lambda p: (calls.append(p), True)[1])
    m._on_app_state(Qt.ApplicationActive)
    wait_until(lambda: not m._sweeping, 5)
    n1 = len(calls)
    assert n1 > 0
    m._on_app_state(Qt.ApplicationActive)  # одразу вдруге — тротлінг
    time.sleep(0.05)
    _qapp.processEvents()
    assert len(calls) == n1, "повторний sweep раніше ніж за 3с — заборонений"


def test_trash_runs_in_background(tmp_path, monkeypatch):
    for top in ("A", "B"):
        make(tmp_path / "t" / top / "x.bin", b"X" * 900)
    r = core.scan([str(tmp_path / "t")])
    m = app_mod.Main()
    m.result = r
    m.m_files.set_groups(r.file_groups)
    victim = r.file_groups[0].paths[0]
    m.m_files.checked.add(victim)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    gate = threading.Event()
    trashed = []

    def slow_trash(paths, **_kwargs):
        gate.wait(5)
        trashed.extend(paths)
        return []

    monkeypatch.setattr(app_mod, "to_trash", slow_trash)
    t0 = time.monotonic()
    m.delete_checked(m.m_files)
    assert time.monotonic() - t0 < 0.3, "Кошик мусить працювати у фоні"
    assert "Кошик" in m.status.text()
    gate.set()
    assert wait_until(lambda: trashed == [victim])
    assert wait_until(lambda: victim not in m.m_files.checked)


def test_history_load_is_async(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    for top in ("A", "B"):
        make(tmp_path / "t" / top / "x.bin", b"X" * 800)
    r = core.scan([str(tmp_path / "t")])
    p = session.save_session(r, [str(tmp_path / "t")])
    m = app_mod.Main()
    gate = threading.Event()
    real_load = session.load_session

    def slow_load(path, **kwargs):
        gate.wait(5)
        return real_load(path, **kwargs)

    monkeypatch.setattr(app_mod.session, "load_session", slow_load)
    t0 = time.monotonic()
    m._load_session(p, [str(tmp_path / "t")])
    assert time.monotonic() - t0 < 0.3, "завантаження сесії мусить бути фоновим"
    gate.set()
    assert wait_until(lambda: m.result is not None and m.result.file_groups)


def test_history_load_cancel_keeps_current_result(monkeypatch):
    m = app_mod.Main()
    current = core.ScanResult(live=True)
    m.result = current
    entered = threading.Event()

    def cancellable_load(_path, *, cancel, progress):
        entered.set()
        while not cancel.wait(0.01):
            progress("Читаю сесію", 1, 0)
        raise OSError(errno.ECANCELED, "cancelled")

    monkeypatch.setattr(app_mod.session, "load_session", cancellable_load)

    m._load_session("/slow-session.json", ["/unused"])
    assert entered.wait(1)
    m.cancel_scan()

    assert wait_until(lambda: not m._loading)
    assert m.result is current
    assert "скасовано" in m.status.text().casefold()
    assert not m.operation_controls.isVisible()
    m.close()


def test_large_search_computation_is_async(monkeypatch):
    m = app_mod.Main()
    groups = [
        core.FileGroup(
            10,
            "digest",
            [f"/root/file-{i:05d}.bin" for i in range(20_001)],
        )
    ]
    m.m_files.set_groups(groups)
    edit = QLineEdit()
    edit.setText("file-20000")
    gate = threading.Event()
    real_prepare = m.m_files.prepare_filter

    def delayed_prepare(text, cancel):
        serial, job = real_prepare(text, cancel)

        def delayed():
            gate.wait(5)
            return job()

        return serial, delayed

    monkeypatch.setattr(m.m_files, "prepare_filter", delayed_prepare)
    started = time.monotonic()
    m._filter_model(m.m_files, edit)
    assert time.monotonic() - started < 0.3
    assert m.m_files._filter_text == ""
    gate.set()
    assert wait_until(lambda: m.m_files._filter_text == "file-20000")
    assert m.m_files.rowCount() == 1
    m.close()


def test_close_never_waits_for_background_worker():
    m = app_mod.Main()
    m.show()
    gate = threading.Event()
    m._ui_bg_run(lambda: gate.wait(5), lambda _value: None)
    assert wait_until(lambda: any(w.isRunning() for w in m._ui_bg))

    started = time.monotonic()
    m.close()
    assert time.monotonic() - started < 0.3
    assert m._closing and m.isVisible()

    gate.set()
    assert wait_until(lambda: not m.isVisible())


def test_close_suppresses_queued_background_followup():
    m = app_mod.Main()
    m.show()
    gate = threading.Event()
    callbacks = []
    m._bg_run(lambda: gate.wait(5), lambda _value: callbacks.append("followup"))
    assert wait_until(lambda: any(w.isRunning() for w in m._bg))

    m.close()
    gate.set()

    assert wait_until(lambda: not m.isVisible())
    assert callbacks == []


def test_scan_failure_during_close_never_opens_dialog(monkeypatch):
    m = app_mod.Main()
    m._closing = True
    dialogs = []
    monkeypatch.setattr(
        QMessageBox, "critical",
        lambda *args, **kwargs: dialogs.append(args))

    m.on_scan_failed("network volume disconnected")

    assert dialogs == []


def test_merge_planning_is_async_and_mutually_exclusive(tmp_path, monkeypatch):
    source = tmp_path / "A"
    target = tmp_path / "B"
    make(source / "shared.bin", b"same")
    make(target / "shared.bin", b"same")
    result = core.scan([str(source), str(target)])
    pair = core.SimPair(str(source), str(target), 100.0, 4)
    m = app_mod.Main()
    m.result = result
    gate = threading.Event()
    real_plan = core.merge_plan

    def slow_plan(*args, **kwargs):
        gate.wait(5)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(app_mod.core, "merge_plan", slow_plan)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No)
    busy_messages = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *args, **kwargs: busy_messages.append(str(args[-1])))

    started = time.monotonic()
    m._run_sim_merge(pair, into_a=True, res=result)
    assert time.monotonic() - started < 0.3
    assert wait_until(lambda: m.merge_worker and m.merge_worker.isRunning())

    m.start_scan()
    assert busy_messages and "поточної операції" in busy_messages[-1]

    gate.set()
    assert wait_until(lambda: not m._merging)
    m.close()


def test_merge_device_probe_runs_outside_gui_thread(tmp_path, monkeypatch):
    source = tmp_path / "A"
    target = tmp_path / "B"
    make(source / "unique.bin", b"unique")
    target.mkdir()
    result = core.scan([str(source), str(target)])
    pair = core.SimPair(str(source), str(target), 10.0, 1)
    main = app_mod.Main()
    main.result = result
    gate = threading.Event()
    probe_threads = []

    def slow_probe(_source, _target):
        probe_threads.append(threading.current_thread())
        gate.wait(5)
        return True

    monkeypatch.setattr(app_mod, "_same_device", slow_probe)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.No)

    started = time.monotonic()
    main._run_sim_merge(pair, into_a=False, res=result)
    assert time.monotonic() - started < 0.3
    assert wait_until(lambda: bool(probe_threads))
    assert probe_threads[0] is not threading.main_thread()
    assert not main.results_operation_banner.isHidden()
    assert "Файли ще не змінюються" in main.results_operation_text.text()

    gate.set()
    assert wait_until(lambda: not main._merging)
    main.close()


def test_merge_preparation_reports_progress_inside_large_file(
        tmp_path, monkeypatch):
    source = tmp_path / "A"
    target = tmp_path / "B"
    make(source / "large.bin", b"L" * (3 * 1024 * 1024 + 17))
    target.mkdir()
    result = core.scan([str(source), str(target)])
    worker = app_mod.MergePreparationWorker(
        result, str(source), str(target))
    progress = []
    completed = []
    worker.progress.connect(
        lambda phase, done, total: progress.append((phase, done, total)))
    worker.done.connect(completed.append)
    clock = [0.0]

    def advancing_clock():
        clock[0] += 0.11
        return clock[0]

    monkeypatch.setattr(app_mod.time, "monotonic", advancing_clock)

    worker.run()

    assert completed and len(completed[0]) == 5
    assert len(progress) >= 3
    assert any(0 < done < total for _phase, done, total in progress)
    assert all("large.bin" in phase for phase, _done, _total in progress)


def test_merge_preparation_can_be_paused_and_cancelled_before_mutation(
        tmp_path, monkeypatch):
    source = tmp_path / "A"
    target = tmp_path / "B"
    make(source / "unique.bin", b"unique")
    source.mkdir(exist_ok=True)
    target.mkdir()
    result = core.scan([str(source), str(target)])
    pair = core.SimPair(str(source), str(target), 10.0, 1)
    m = app_mod.Main()
    m.result = result
    entered = threading.Event()

    def controlled_verify(
            path, expected=None, cancel=None, pause=None, progress=None):
        entered.set()
        while not cancel.is_set():
            time.sleep(0.005)
        return "0" * 64, os.stat(path)

    monkeypatch.setattr(app_mod.core, "verify_current_file", controlled_verify)

    m._run_sim_merge(pair, into_a=False, res=result)
    assert wait_until(entered.is_set)
    m.toggle_pause()
    assert m.merge_worker.pause.is_set()
    m.cancel_scan()

    assert wait_until(lambda: m.merge_worker is None)
    assert not m._merging
    assert (source / "unique.bin").exists()
    assert not (target / "unique.bin").exists()
    assert "Жодного файла не змінено" in m.status.text()
    m.close()


def test_stale_merge_preparation_callback_cannot_touch_replaced_result(
        tmp_path, monkeypatch):
    source = tmp_path / "A"
    target = tmp_path / "B"
    make(source / "unique.bin", b"unique")
    target.mkdir()
    result = core.scan([str(source), str(target)])
    pair = core.SimPair(str(source), str(target), 10.0, 1)
    m = app_mod.Main()
    m.result = result
    monkeypatch.setattr(
        app_mod.MergePreparationWorker, "start", lambda _worker: None)
    questions = []
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: (
            questions.append(args), QMessageBox.StandardButton.No)[1])

    m._run_sim_merge(pair, into_a=False, res=result)
    stale_worker = m.merge_worker
    replacement = core.ScanResult(live=False)
    m.result = replacement
    m._session_context_token += 1
    m._merging = False
    m.status.setText("нова сесія активна")

    stale_worker.done.emit(([], 0, {}, True))
    stale_worker.failed.emit("stale merge failure")
    _qapp.processEvents()

    assert m.result is replacement
    assert m.merge_worker is None
    assert questions == []
    assert m.status.text() == "нова сесія активна"
    m.close()


def test_external_volume_report_error_is_visible_and_not_false_success(
        tmp_path, monkeypatch):
    for top in ("A", "B"):
        make(tmp_path / top / "same.bin", b"same")
    m = app_mod.Main()
    m.result = core.scan([str(tmp_path / "A"), str(tmp_path / "B")])
    destination = tmp_path / "external" / "report.csv"
    destination.parent.mkdir()
    destination.write_bytes(b"previous-report")
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "CSV (*.csv)"))

    def disconnected(*_args, **_kwargs):
        raise OSError(errno.ENOTCONN, "network volume disconnected")

    monkeypatch.setattr(app_mod.reports, "export_csv", disconnected)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, text: warnings.append((title, text)))

    started = time.monotonic()
    m.export_report("csv")
    assert time.monotonic() - started < 0.3
    assert wait_until(lambda: warnings)
    assert destination.read_bytes() == b"previous-report"
    assert "network volume disconnected" in warnings[0][1]
    assert "не створено" in m.status.text()
    assert "збережено" not in m.status.text()
    m.close()


def test_external_volume_diagnostics_error_restores_clear_status(
        tmp_path, monkeypatch):
    m = app_mod.Main()
    destination = tmp_path / "external" / "diagnostics.zip"
    destination.parent.mkdir()
    destination.write_bytes(b"previous-diagnostics")
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName",
        lambda *_args, **_kwargs: (str(destination), "ZIP (*.zip)"))
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.No)

    def full_volume(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "external volume is full")

    monkeypatch.setattr(
        app_mod.diagnostics, "export_diagnostics_bundle", full_volume)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, text: warnings.append((title, text)))

    started = time.monotonic()
    m.export_diagnostics()
    assert time.monotonic() - started < 0.3
    assert wait_until(lambda: warnings)
    assert destination.read_bytes() == b"previous-diagnostics"
    assert "external volume is full" in warnings[0][1]
    assert "не створено" in m.status.text()
    assert "збережено" not in m.status.text()
    m.close()
