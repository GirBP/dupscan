"""Smart Refresh: historical snapshots become current without trusting stale data.

These tests deliberately exercise the small public contracts used by the UI:
the pure cache-seeding plan, root samples, the background worker and refresh
button lifecycle.  They never walk a real mounted volume or invoke Finder.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from copy import deepcopy

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.infra.cache as cache  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


def _historical(old_root: str, other_root: str | None = None) -> core.ScanResult:
    """A minimal loaded session with full, unique and malformed classes."""
    result = core.ScanResult(live=False)
    paths = [
        (os.path.join(old_root, "keep.bin"), 10, 101, 102, 7, 70, f"10:{_DIGEST_A}"),
        (os.path.join(old_root, "unique.bin"), 11, 201, 202, 7, 71, "u:11"),
        (os.path.join(old_root, "partial.bin"), 12, 301, 302, 7, 72, "12:not-a-digest"),
    ]
    if other_root:
        paths.append((os.path.join(other_root, "other.bin"), 13, 401, 402, 8, 80,
                      f"13:{_DIGEST_B}"))
    for path, size, mtime, ctime, dev, ino, file_class in paths:
        result.file_meta[path] = core.FileInfo(
            path, size, mtime, 0, ctime, dev, ino)
        result.file_class[path] = file_class
    return result


def test_refresh_seed_plan_only_translates_trusted_full_digests_and_dedupes():
    old_root = "/historical/volume"
    new_root = "/current/volume"
    previous = _historical(old_root)
    # Defensive dedupe: a legacy non-normalized duplicate must not seed twice.
    alias = os.path.join(old_root, "nested", "..", "keep.bin")
    previous.file_meta[alias] = core.FileInfo(alias, 10, 101, 0, 102, 7, 70)
    previous.file_class[alias] = f"10:{_DIGEST_A}"

    rows = app_mod.build_session_refresh_cache_rows(
        previous, (old_root,), {old_root: new_root})

    assert rows == [
        (os.path.join(new_root, "keep.bin"), "f", 10, 101, 102, 7, 70, _DIGEST_A),
    ]
    # The helper is a plan only: no historical path/object may be mutated.
    assert previous.file_meta[os.path.join(old_root, "keep.bin")].path == (
        os.path.join(old_root, "keep.bin"))


def test_refresh_seed_plan_handles_multiple_roots_without_guessing_parent():
    old_a, old_b = "/old/a", "/old/b"
    new_a, new_b = "/new/a", "/new/b"
    previous = _historical(old_a, old_b)

    rows = app_mod.build_session_refresh_cache_rows(
        previous, (old_a, old_b), {old_a: new_a, old_b: new_b})

    assert {row[0] for row in rows} == {
        os.path.join(new_a, "keep.bin"), os.path.join(new_b, "other.bin")}
    assert all(not path.startswith("/new/") or path.startswith((new_a, new_b))
               for path, *_rest in rows)


def test_root_samples_are_deterministic_per_root_and_never_escape_root():
    old_a, old_b = "/old/a", "/old/b"
    previous = _historical(old_a, old_b)
    previous.file_meta["/outside/nope.bin"] = core.FileInfo(
        "/outside/nope.bin", 1, 1, 0, 1, 1, 1)

    samples = app_mod.session_path_samples(previous, (old_a, old_b), per_root=1)

    assert samples == app_mod.session_path_samples(
        previous, (old_a, old_b), per_root=1)
    assert set(samples) == {old_a, old_b}
    assert all(len(paths) == 1 for paths in samples.values())
    assert all(
        os.path.commonpath((root, paths[0])) == root
        for root, paths in samples.items()
    )
    assert all(
        "/outside/nope.bin" not in paths for paths in samples.values())


def test_directory_only_root_samples_fill_bounded_representative_set():
    root = "/old/directory-only"
    previous = core.ScanResult(live=False)
    previous.dir_ok = {
        os.path.join(root, f"folder-{index:02d}"): True
        for index in range(20)
    }

    samples = app_mod.session_path_samples(previous, (root,), per_root=8)

    assert len(samples[root]) == 8
    assert len(set(samples[root])) == 8
    assert samples == app_mod.session_path_samples(
        previous, (root,), per_root=8)
    assert all(os.path.commonpath((root, path)) == root for path in samples[root])


def test_refresh_worker_seeds_cache_runs_exact_resolved_roots_and_saves_new_session(
        tmp_path, monkeypatch):
    old_root = "/old/volume"
    new_root = str(tmp_path / "current")
    os.makedirs(new_root)
    previous = _historical(old_root)
    captured: dict[str, object] = {"rows": []}

    class Cache:
        def put_many(self, rows):
            captured["rows"].extend(rows)

        def close(self):
            captured["closed"] = True

    fresh = core.ScanResult(live=True)

    def fake_scan(roots, **kwargs):
        captured["scan"] = (list(roots), kwargs)
        return fresh

    monkeypatch.setattr(
        app_mod.cache.HashCache, "open", lambda **_kwargs: Cache())
    monkeypatch.setattr(app_mod.core, "scan", fake_scan)
    monkeypatch.setattr(app_mod.session, "save_session",
                        lambda result, roots, **kwargs: captured.setdefault(
                            "saved", (result, list(roots), kwargs)))
    worker = app_mod.SessionRefreshWorker(
        [new_root], previous, session_roots=(old_root,),
        root_map={old_root: new_root},
        source_session_path="/sessions/historical.json")
    done = []
    worker.done.connect(done.append)

    worker.run()

    roots, kwargs = captured["scan"]
    assert roots == [new_root]
    assert old_root not in roots
    assert kwargs["profile"] is None
    assert kwargs["cancel"] is worker.cancel
    assert kwargs["pause"] is worker.pause
    assert captured["rows"] == [
        (os.path.join(new_root, "keep.bin"), "f", 10, 101, 102, 7, 70, _DIGEST_A)
    ]
    assert captured["saved"][0] is fresh
    assert captured["saved"][1] == [new_root]
    assert captured["saved"][2]["preserve_paths"] == (
        "/sessions/historical.json",)
    assert done == [fresh]
    assert worker.reused_hashes == 1


def test_refresh_worker_cancel_keeps_snapshot_and_never_saves_or_succeeds(
        tmp_path, monkeypatch):
    old_root = "/old/volume"
    new_root = str(tmp_path / "current")
    os.makedirs(new_root)
    previous = _historical(old_root)
    original = deepcopy(previous)
    scans, saves, done = [], [], []
    monkeypatch.setattr(app_mod.core, "scan", lambda *a, **k: scans.append((a, k)))
    monkeypatch.setattr(app_mod.session, "save_session", lambda *a, **k: saves.append(a))
    worker = app_mod.SessionRefreshWorker(
        [new_root], previous, session_roots=(old_root,),
        root_map={old_root: new_root})
    worker.done.connect(done.append)
    worker.cancel.set()

    worker.run()

    assert scans == []
    assert saves == []
    assert done == []
    assert previous == original


def test_refresh_worker_rejects_root_replaced_by_symlink_before_scan(
        tmp_path, monkeypatch):
    old_root = "/old/volume"
    selected_root = tmp_path / "selected"
    target_root = tmp_path / "target"
    selected_root.mkdir()
    target_root.mkdir()
    # The preflight could have selected a real directory. A later replacement
    # must be rejected at the worker boundary before cache/scan access.
    selected_root.rmdir()
    try:
        selected_root.symlink_to(target_root, target_is_directory=True)
    except OSError as error:  # pragma: no cover - unusual filesystem policy
        pytest.skip(f"symlink unavailable: {error}")
    monkeypatch.setattr(
        app_mod.cache.HashCache, "open",
        lambda: pytest.fail("invalid root must fail before opening cache"))
    monkeypatch.setattr(
        app_mod.core, "scan",
        lambda *_args, **_kwargs: pytest.fail(
            "invalid root must fail before core.scan"))
    worker = app_mod.SessionRefreshWorker(
        [str(selected_root)], _historical(old_root),
        session_roots=(old_root,), root_map={old_root: str(selected_root)})
    failures = []
    worker.failed.connect(failures.append)

    worker.run()

    assert failures
    assert "реальною текою" in failures[0]
    assert "symlink" in failures[0]


def test_refresh_worker_rechecks_identity_after_cache_seeding(
        tmp_path, monkeypatch):
    old_root = "/old/volume"
    selected_root = tmp_path / "selected"
    target_root = tmp_path / "target"
    selected_root.mkdir()
    target_root.mkdir()

    class Cache:
        def put_many(self, _rows):
            selected_root.rmdir()
            selected_root.symlink_to(target_root, target_is_directory=True)

        def close(self):
            pass

    monkeypatch.setattr(
        app_mod.cache.HashCache, "open", lambda **_kwargs: Cache())
    monkeypatch.setattr(
        app_mod.core, "scan",
        lambda *_args, **_kwargs: pytest.fail(
            "root changed during cache seed must not reach core.scan"))
    worker = app_mod.SessionRefreshWorker(
        [str(selected_root)], _historical(old_root),
        session_roots=(old_root,), root_map={old_root: str(selected_root)})
    failures = []
    worker.failed.connect(failures.append)

    worker.run()

    assert failures
    assert (
        "реальною текою" in failures[0]
        or "замінено після перевірки" in failures[0]
    )


def test_refresh_worker_rechecks_root_identity_after_scan(
        tmp_path, monkeypatch):
    root = tmp_path / "selected"
    old_root = tmp_path / "selected-old"
    root.mkdir()
    fresh = core.ScanResult(live=True)
    saves = []

    class Cache:
        def put_many(self, _rows):
            pass

        def close(self):
            pass

    def replacing_scan(*_args, **_kwargs):
        root.rename(old_root)
        root.mkdir()
        return fresh

    monkeypatch.setattr(
        app_mod.cache.HashCache, "open", lambda **_kwargs: Cache())
    monkeypatch.setattr(app_mod.core, "scan", replacing_scan)
    monkeypatch.setattr(
        app_mod.session, "save_session",
        lambda *_args, **_kwargs: saves.append(True))
    worker = app_mod.SessionRefreshWorker(
        [str(root)], _historical("/old/root"),
        session_roots=("/old/root",))
    completed, failures = [], []
    worker.done.connect(completed.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert completed == []
    assert failures and "замінено" in failures[0]
    assert saves == []


def test_cancel_arriving_after_atomic_refresh_save_reports_success(
        tmp_path, monkeypatch):
    root = tmp_path / "selected"
    root.mkdir()
    fresh = core.ScanResult(live=True)

    class Cache:
        def put_many(self, _rows):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        app_mod.cache.HashCache, "open", lambda **_kwargs: Cache())
    monkeypatch.setattr(
        app_mod.core, "scan", lambda *_args, **_kwargs: fresh)
    worker = app_mod.SessionRefreshWorker(
        [str(root)], _historical("/old/root"),
        session_roots=("/old/root",))

    def save_then_cancel(*_args, **_kwargs):
        worker.cancel.set()
        return "/new/live-session.json.gz"

    monkeypatch.setattr(app_mod.session, "save_session", save_then_cancel)
    completed, cancelled = [], []
    worker.done.connect(completed.append)
    worker.cancelled.connect(lambda: cancelled.append(True))

    worker.run()

    assert completed == [fresh]
    assert cancelled == []
    assert worker.saved_session_path == "/new/live-session.json.gz"


def test_refresh_worker_does_not_report_success_when_new_session_is_not_saved(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    fresh = core.ScanResult(live=True)

    class Cache:
        def put_many(self, _rows):
            pass

        def close(self):
            pass

    monkeypatch.setattr(
        app_mod.cache.HashCache, "open", lambda **_kwargs: Cache())
    monkeypatch.setattr(app_mod.core, "scan", lambda *_args, **_kwargs: fresh)
    monkeypatch.setattr(app_mod.session, "save_session", lambda *_a, **_k: "")
    worker = app_mod.SessionRefreshWorker(
        [str(root)], _historical("/old/root"),
        session_roots=("/old/root",))
    completed, failures = [], []
    worker.done.connect(completed.append)
    worker.failed.connect(failures.append)

    worker.run()

    assert completed == []
    assert failures
    assert "не вдалося зберегти" in failures[0]


def test_refresh_button_is_only_available_for_historical_or_partial_and_controls_worker():
    main = app_mod.Main()
    main.show()
    historical = core.ScanResult(live=False)
    main.result = historical
    main._finish_model_refresh(historical)
    main._show_task("results")
    _qapp.processEvents()
    assert main.session_rescan_panel.isVisibleTo(main)
    assert main.b_refresh_session.isVisibleTo(main)
    assert main.b_refresh_session.isEnabled()
    assert main.b_refresh_session.accessibleName()
    assert main.b_refresh_session.accessibleDescription()

    class RefreshWorker:
        def __init__(self):
            self.pause = threading.Event()
            self.cancel = threading.Event()

        @staticmethod
        def isRunning():
            return True

    main.refresh_worker = RefreshWorker()
    main.toggle_pause()
    assert main.refresh_worker.pause.is_set()
    main.cancel_scan()
    assert main.refresh_worker.cancel.is_set()
    assert "онов" in main.status.text().casefold()

    live = core.ScanResult(live=True)
    main.result = live
    main.refresh_worker = None
    main._finish_model_refresh(live)
    _qapp.processEvents()
    assert not main.session_rescan_panel.isVisibleTo(main)
    assert not main.b_refresh_session.isVisibleTo(main)
    main.close()


def test_real_cache_reuses_unchanged_digest_but_changed_identity_is_rehashed(
        tmp_path, monkeypatch):
    root = tmp_path / "tree"
    first = root / "first.bin"
    second = root / "second.bin"
    first.parent.mkdir(parents=True)
    # > PARTIAL: інакше одноразова проба читає файл цілком і "full"-виклик
    # легітимно не потрібен (однопрохідна воронка 2.14)
    payload = b"same-content" * 100 + b"Z" * core.PARTIAL
    first.write_bytes(payload)
    second.write_bytes(payload)
    initial = core.scan([str(root)])
    historical = deepcopy(initial)
    historical.live = False
    initial_class = historical.file_class[str(first)]
    disk_cache = cache.HashCache.open(str(tmp_path / "cache"))
    disk_cache.put_many(app_mod.build_session_refresh_cache_rows(
        historical, (str(root),), {}))
    calls: list[tuple[str, bool]] = []
    real_hash = core._hash_file

    def traced_hash(path, size, full, cancel, *args, **kwargs):
        calls.append((path, full))
        return real_hash(path, size, full, cancel, *args, **kwargs)

    monkeypatch.setattr(core, "_hash_file", traced_hash)
    unchanged = core.scan([str(root)], cache=disk_cache)
    assert unchanged.file_class[str(first)] == initial_class
    assert not [path for path, full in calls if full], "full digest must be reused"

    # Same-size replacement: size alone is never sufficient to trust history.
    replacement = b"new-content!" * 100 + b"Q" * core.PARTIAL  # > PARTIAL
    first.write_bytes(replacement)
    third = root / "third.bin"
    third.write_bytes(replacement)
    calls.clear()
    changed = core.scan([str(root)], cache=disk_cache)
    disk_cache.close()

    assert str(first) in {path for path, full in calls if full}
    assert changed.file_class[str(first)] != initial_class
    assert changed.file_class[str(first)] == changed.file_class[str(third)]
    assert historical.file_class[str(first)] == initial_class


def test_refresh_worker_reflects_add_delete_without_mutating_historical(
        tmp_path, monkeypatch):
    root = tmp_path / "tree"
    first, gone, added = root / "first.bin", root / "gone.bin", root / "added.bin"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"same" * 400)
    gone.write_bytes(b"same" * 400)
    historical = core.scan([str(root)])
    historical.live = False
    before = deepcopy(historical)
    gone.unlink()
    added.write_bytes(b"same" * 400)
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    saved = []
    monkeypatch.setattr(
        app_mod.session, "save_session",
        lambda *args, **kwargs: (saved.append(args), "/new/session.json")[1])
    worker = app_mod.SessionRefreshWorker(
        [str(root)], historical, session_roots=(str(root),))
    completed = []
    worker.done.connect(completed.append)

    worker.run()

    assert completed and completed[0].live
    current = completed[0]
    assert set(current.file_meta) == {str(first), str(added)}
    assert set(historical.file_meta) == set(before.file_meta)
    assert worker.stats["added"] == 1
    assert worker.stats["removed"] == 1
    assert saved and saved[0][0] is current


def test_moved_root_preflight_starts_worker_only_for_explicit_selected_root(
        tmp_path, monkeypatch):
    old_root = str(tmp_path / "old-volume")
    selected_root = tmp_path / "mounted" / "actual-root"
    selected_file = selected_root / "keep.bin"
    selected_file.parent.mkdir(parents=True)
    selected_file.write_bytes(b"0123456789")
    previous = _historical(old_root)
    main = app_mod.Main()
    main.result = previous
    main._loaded_session_roots = (old_root,)
    main._last_roots = [old_root]
    started = []

    def synchronous_bg(fn, on_ok, on_err=None):
        try:
            on_ok(fn())
        except Exception as error:  # pragma: no cover - assertion aid
            if on_err:
                on_err(str(error))
            else:
                raise

    monkeypatch.setattr(main, "_ui_bg_run", synchronous_bg)
    monkeypatch.setattr(main, "_confirm_session_refresh", lambda *_args: True)
    monkeypatch.setattr(
        main, "_confirm_manual_refresh_root",
        lambda *_args, **_kwargs: True)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *_args, **_kwargs: [str(selected_root)])
    monkeypatch.setattr(
        app_mod.SessionRefreshWorker, "start",
        lambda worker: started.append(list(worker.roots)))

    main.refresh_loaded_session()

    assert started == [[str(selected_root)]]
    assert old_root not in started[0]
    assert str(tmp_path / "mounted") not in started[0]
    main.refresh_worker = None
    main.close()


def test_stale_refresh_callback_cannot_replace_newly_loaded_result(tmp_path, monkeypatch):
    root = str(tmp_path / "root")
    os.makedirs(root)
    previous = _historical(root)
    main = app_mod.Main()
    main.result = previous
    main._loaded_session_roots = (root,)
    main._refresh_preflighting = True
    monkeypatch.setattr(app_mod.SessionRefreshWorker, "start", lambda _worker: None)

    main._start_session_refresh(previous, (root,), [root], main._session_context_token)
    stale_worker = main.refresh_worker
    newly_loaded = core.ScanResult(live=False)
    main.result = newly_loaded
    main._session_context_token += 1
    previous_status = main.status.text()
    stale_worker.progress.emit("застарілий прогрес", 99, 100)
    stale_worker.done.emit(core.ScanResult(live=True))
    _qapp.processEvents()

    assert main.result is newly_loaded
    assert main._loaded_session_roots == (root,)
    assert main.status.text() == previous_status
    main.refresh_worker = None
    main.close()
