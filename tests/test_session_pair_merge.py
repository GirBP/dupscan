"""A historical session may safely merge only after a fresh A/B pair scan."""

import os
import sys
import tempfile
import threading
import time
from copy import deepcopy

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _wait_until(condition, timeout=10.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        _qapp.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


def _session_with_pair(tmp_path):
    tree = tmp_path / "tree"
    _make(tree / "A" / "shared.bin", b"shared" * 900)
    _make(tree / "A" / "only-a.bin", b"A" * 800)
    _make(tree / "B" / "shared-copy.bin", b"shared" * 900)
    _make(tree / "B" / "only-b.bin", b"B" * 1_100)
    # This file must not be read by the on-demand merge verification.
    _make(tree / "unrelated" / "terabyte-placeholder.bin", b"unrelated" * 500)
    result = core.scan([str(tree)])
    saved = session.save_session(result, [str(tree)], base_dir=str(tmp_path / "data"))
    loaded = session.load_session(saved)
    pair = next(
        item for item in loaded.sim_pairs
        if {item.dir_a, item.dir_b} == {str(tree / "A"), str(tree / "B")}
    )
    return tree, loaded, pair


def test_saved_session_merge_verifies_only_the_chosen_pair(tmp_path, monkeypatch):
    tree, loaded, pair = _session_with_pair(tmp_path)
    historical_state = deepcopy(loaded)
    main = app_mod.Main()
    main.result = loaded
    main.m_sim.set_pairs(loaded.sim_pairs)
    scan_calls = []
    real_scan = core.scan

    def traced_scan(roots, *args, **kwargs):
        scan_calls.append(tuple(os.path.abspath(path) for path in roots))
        return real_scan(roots, *args, **kwargs)

    monkeypatch.setattr(app_mod.core, "scan", traced_scan)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1])

    main._sim_merge(pair, into_a=True)  # B → A

    assert _wait_until(lambda: trashed == [pair.dir_b])
    assert len(scan_calls) == 1
    assert set(scan_calls[0]) == {pair.dir_a, pair.dir_b}
    assert str(tree) not in scan_calls[0]
    assert (tree / "A" / "only-b.bin").exists()
    assert loaded == historical_state, "історична сесія має лишитися незмінною"
    main.close()


def test_snapshot_ui_offers_pair_merge_instead_of_demanding_full_rescan():
    """Знімок мусить казати правду про те, що з нього МОЖНА зробити.

    Механіка була на місці й покрита тестами вище: `_sim_merge` на неживій
    сесії сам пересканує РІВНО обрані A/B. А банер вимагав спершу
    «запустити інтелектуальний рескан, щоб безпечно працювати з
    дублікатами», і бейдж казав просто «ЛИШЕ ПЕРЕГЛЯД» — разом це читалось
    як заборона злиття, якої насправді не існує.

    «Лише перегляд» лишається правдою для ГРУП: у групи немає обмеженої
    області, яку можна чесно перевірити наново, тому там і далі потрібен
    повний рескан.
    """
    result = core.ScanResult(
        file_groups=[core.FileGroup(100, "f", ["/a", "/b"])],
        files_seen=2,
        live=False,
    )
    main = app_mod.Main()
    main.result = result
    main.m_files.set_groups(result.file_groups)
    main._finish_model_refresh(result)

    banner = main.session_rescan_text.text()
    assert "злити" in banner.casefold(), "банер не називає доступну дію"
    assert "A і B" in banner, "банер не каже, що перевіряється лише пара"

    summary = main.summary.text()
    assert "ЛИШЕ ПЕРЕГЛЯД для груп" in summary
    assert "пару A/B" in summary
    main.close()


def test_trashed_source_pair_row_is_visibly_tagged(tmp_path, monkeypatch):
    """Після «теку в Кошик» рядок пари мусить ПОКАЗУВАТИ, що сталося.

    Історичний знімок незмінний навмисно (інваріант: сесія — доказ того,
    що було на диску в момент скану). Але «незмінний знімок» не означає
    «німий інтерфейс»: власник побачив теку в Кошику І той самий рядок у
    списку без жодної позначки — виглядало як збій. Мітка живе в моделі
    (view-шар), сесії не торкається і зникає разом із нею.
    """
    tree, loaded, pair = _session_with_pair(tmp_path)
    main = app_mod.Main()
    main.result = loaded
    main.m_sim.set_pairs(loaded.sim_pairs)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1])

    main._sim_merge(pair, into_a=True)  # B → A

    assert _wait_until(lambda: trashed == [pair.dir_b])
    key = app_mod.SimModel.pair_key(pair)
    assert _wait_until(lambda: key in main.m_sim.tags), (
        "рядок злитої пари лишився без мітки")
    assert "Кошик" in main.m_sim.tags[key]
    main.close()


def test_similarity_root_rows_are_explicitly_labelled_a_and_b():
    model = app_mod.SimModel()
    pair = core.SimPair("/Volumes/one/Camera", "/Volumes/two/Camera", 50.0, 10)
    model.set_pairs([pair])

    root = model.index(0, 0)
    assert model.headerData(0, Qt.Horizontal, Qt.DisplayRole).startswith("A")
    assert model.headerData(1, Qt.Horizontal, Qt.DisplayRole).startswith("B")
    assert str(model.data(root, Qt.DisplayRole)).startswith("A · /Volumes/one/")
    assert str(model.data(root.siblingAtColumn(1), Qt.DisplayRole)).startswith(
        "B · /Volumes/two/")


def test_directory_tree_complete_rejects_a_missing_child(tmp_path):
    _make(tmp_path / "checked" / "nested" / "file.bin", b"ok")
    result = core.scan([str(tmp_path / "checked")])
    assert core.directory_tree_complete(result, str(tmp_path / "checked"))
    result.dir_ok[str(tmp_path / "checked" / "nested")] = False
    assert not core.directory_tree_complete(result, str(tmp_path / "checked"))


def test_pair_worker_rejects_nested_roots_before_scan(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    scans = []
    monkeypatch.setattr(
        app_mod.core, "scan", lambda *args, **kwargs: scans.append(args))
    worker = app_mod.PairVerificationWorker(str(parent), str(child))
    errors = []
    worker.failed.connect(errors.append)

    worker.run()

    assert scans == []
    assert errors and "невкладеними" in errors[0]


def test_pair_worker_rejects_symlink_root_before_scan(tmp_path, monkeypatch):
    real_a = tmp_path / "real-a"
    real_b = tmp_path / "real-b"
    real_a.mkdir()
    real_b.mkdir()
    alias_a = tmp_path / "alias-a"
    os.symlink(real_a, alias_a)
    scans = []
    monkeypatch.setattr(
        app_mod.core, "scan", lambda *args, **kwargs: scans.append(args))
    worker = app_mod.PairVerificationWorker(str(alias_a), str(real_b))
    errors = []
    worker.failed.connect(errors.append)

    worker.run()

    assert scans == []
    assert errors and "символічними" in errors[0]


def test_pair_worker_uses_exact_roots_and_strict_default_scan(tmp_path, monkeypatch):
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    dir_a.mkdir()
    dir_b.mkdir()
    calls = []

    class Cache:
        def close(self):
            pass

    def scan(roots, **kwargs):
        calls.append((roots, kwargs))
        return core.ScanResult()

    monkeypatch.setattr(
        app_mod.cache.HashCache, "open", lambda **_kwargs: Cache())
    monkeypatch.setattr(app_mod.core, "scan", scan)
    worker = app_mod.PairVerificationWorker(str(dir_a), str(dir_b))
    worker.run()

    assert len(calls) == 1
    roots, kwargs = calls[0]
    assert roots == [str(dir_a), str(dir_b)]
    assert kwargs["profile"] is None
    assert kwargs["cancel"] is worker.cancel
    assert kwargs["pause"] is worker.pause


def test_pair_cancel_after_done_never_starts_merge(tmp_path, monkeypatch):
    tree, loaded, pair = _session_with_pair(tmp_path)
    main = app_mod.Main()
    main.result = loaded
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    started = []
    monkeypatch.setattr(
        main, "_run_sim_merge", lambda *args, **kwargs: started.append(args))
    monkeypatch.setattr(app_mod.PairVerificationWorker, "start", lambda self: None)

    main._verify_session_pair_then_merge(pair, into_a=True)
    worker = main.pair_worker
    worker.cancel.set()
    fresh = core.ScanResult()
    fresh.dir_ok[pair.dir_a] = True
    fresh.dir_ok[pair.dir_b] = True
    worker.done.emit(fresh)
    _qapp.processEvents()

    assert started == []
    assert not main._merging
    main.close()


def test_finished_pair_callback_cannot_act_after_loading_another_session(
        tmp_path, monkeypatch):
    tree, loaded, pair = _session_with_pair(tmp_path)
    main = app_mod.Main()
    main.result = loaded
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(app_mod.PairVerificationWorker, "start", lambda self: None)
    merges = []
    warnings = []
    monkeypatch.setattr(
        main, "_run_sim_merge", lambda *args, **kwargs: merges.append(args))
    monkeypatch.setattr(
        QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args))

    main._verify_session_pair_then_merge(pair, into_a=True)
    stale_worker = main.pair_worker
    replacement = core.ScanResult(live=False)
    monkeypatch.setattr(
        app_mod.session, "load_session",
        lambda _path, **_kwargs: replacement)
    monkeypatch.setattr(
        app_mod.SessionLoadWorker, "start", lambda worker: worker.run())

    def sync_bg(fn, on_ok, on_err=None):
        try:
            on_ok(fn())
        except Exception as error:  # pragma: no cover - assertion aid
            if on_err is None:
                raise
            on_err(str(error))

    monkeypatch.setattr(main, "_bg_run", sync_bg)
    main._load_session("/new-session.json", [str(tree)])
    main.status.setText("нова сесія активна")
    fresh = core.ScanResult()
    fresh.dir_ok[pair.dir_a] = True
    fresh.dir_ok[pair.dir_b] = True

    stale_worker.done.emit(fresh)
    stale_worker.failed.emit("stale pair failure")
    _qapp.processEvents()

    assert main.result is replacement
    assert main.pair_worker is None
    assert merges == []
    assert warnings == []
    assert main.status.text() == "нова сесія активна"
    assert not main.b_pause.isEnabled()
    assert not main.b_cancel.isEnabled()
    main.close()


def test_incomplete_pair_restores_controls_and_reports_paths(
        tmp_path, monkeypatch):
    tree, loaded, pair = _session_with_pair(tmp_path)
    main = app_mod.Main()
    main.result = loaded
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, message: warnings.append((title, message)))
    monkeypatch.setattr(app_mod.PairVerificationWorker, "start", lambda self: None)
    merges = []
    monkeypatch.setattr(
        main, "_run_sim_merge", lambda *args, **kwargs: merges.append(args))

    main._verify_session_pair_then_merge(pair, into_a=True)
    worker = main.pair_worker
    fresh = core.ScanResult(errors=["[Errno 13] Permission denied: secret.bin"])
    fresh.dir_ok[pair.dir_a] = True
    fresh.dir_ok[pair.dir_b] = False
    # Real read failure (not a deliberate exclusion) — must still block the
    # merge under directory_tree_mergeable, same as directory_tree_complete
    # always did.
    fresh.dir_read_failed.add(pair.dir_b)
    worker.done.emit(fresh)
    _qapp.processEvents()

    assert merges == []
    assert warnings
    message = warnings[0][1]
    assert pair.dir_a in message and pair.dir_b in message
    assert "Permission denied" in message
    assert not main._merging
    assert main.b_scan.isEnabled()
    assert not main.b_pause.isEnabled()
    assert not main.b_cancel.isEnabled()
    main.close()


def test_pair_worker_controls_pause_resume_and_cancel():
    main = app_mod.Main()

    class Worker:
        def __init__(self):
            self.pause = threading.Event()
            self.cancel = threading.Event()

        @staticmethod
        def isRunning():
            return True

    worker = Worker()
    main.pair_worker = worker
    main.toggle_pause()
    assert worker.pause.is_set()
    assert main.b_pause.text() == "Продовжити"
    main.toggle_pause()
    assert not worker.pause.is_set()
    main.cancel_scan()
    assert worker.cancel.is_set()
    assert "вибраної пари" in main.status.text()
    main.pair_worker = None
    main.close()


def test_pair_verification_is_async_and_excludes_overlapping_operations(
        tmp_path, monkeypatch):
    _tree, loaded, pair = _session_with_pair(tmp_path)
    main = app_mod.Main()
    main.result = loaded
    gate = threading.Event()
    real_scan = core.scan

    def slow_scan(roots, *args, **kwargs):
        gate.wait(5)
        return real_scan(roots, *args, **kwargs)

    monkeypatch.setattr(app_mod.core, "scan", slow_scan)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    busy_messages = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda *args, **kwargs: busy_messages.append(str(args[-1])))
    merged = []
    monkeypatch.setattr(
        main, "_run_sim_merge",
        lambda *args, **kwargs: merged.append((args, kwargs)))

    started = time.monotonic()
    main._sim_merge(pair, into_a=True)
    assert time.monotonic() - started < 0.3
    assert _wait_until(lambda: main.pair_worker and main.pair_worker.isRunning())

    main.start_scan()
    assert busy_messages and "поточної операції" in busy_messages[-1]

    gate.set()
    assert _wait_until(lambda: bool(merged))
    main.close()


def test_pair_menu_names_directions_and_finder_sides(tmp_path, monkeypatch):
    dir_a = str(tmp_path / "A")
    dir_b = str(tmp_path / "B")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    pair = core.SimPair(dir_a, dir_b, 50.0, 10)
    main = app_mod.Main()
    main.result = core.ScanResult(live=True)
    main.m_sim.set_pairs([pair])
    root = main.m_sim.index(0, 0)
    monkeypatch.setattr(main.v_sim, "indexAt", lambda _pos: root)

    revealed = []
    monkeypatch.setattr(app_mod, "reveal", lambda path: revealed.append(path))

    menu = main._sim_menu(QPoint(0, 0), execute=False)

    actions = {action.text(): action for action in menu.actions()}
    assert "Злити B → A…" in actions
    assert "Злити A → B…" in actions
    assert "Прибрати спільне з A → Кошик" in actions
    assert "Прибрати спільне з B → Кошик" in actions
    assert "Показати теку A у Finder" in actions
    assert "Показати теку B у Finder" in actions
    assert dir_a in actions["Показати теку A у Finder"].toolTip()
    assert dir_b in actions["Показати теку B у Finder"].toolTip()

    actions["Показати теку A у Finder"].trigger()
    actions["Показати теку B у Finder"].trigger()
    assert _wait_until(lambda: len(revealed) == 2)
    assert set(revealed) == {dir_a, dir_b}
    main.close()


def test_merge_trash_confirmation_repeats_direction_and_full_paths(
        tmp_path, monkeypatch):
    dir_a = str(tmp_path / "A")
    dir_b = str(tmp_path / "B")
    _make(tmp_path / "A" / "shared.bin", b"same")
    _make(tmp_path / "B" / "shared.bin", b"same")
    result = core.scan([dir_a, dir_b])
    pair = core.SimPair(dir_a, dir_b, 100.0, 4)
    main = app_mod.Main()
    main.result = result
    questions = []

    def answer(_parent, title, message, *_args, **_kwargs):
        questions.append((title, message))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", answer)

    main._run_sim_merge(pair, into_a=True, res=result)

    assert _wait_until(lambda: bool(questions))
    title, message = questions[-1]
    assert title == "Тека в Кошик?"
    assert "B → A" in message
    assert f"Тека-джерело (B), яку буде переміщено в Кошик:\n{dir_b}" in message
    assert f"Тека призначення (A), яка залишиться:\n{dir_a}" in message
    main.close()
