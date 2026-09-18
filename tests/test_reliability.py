"""Регресії для пошкоджених файлів, кешу та недовірених сесій."""

import errno
import json
import os
import sys
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import send2trash
from PySide6.QtWidgets import QApplication, QMessageBox

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.cache as cache
import dupscan.domain.core as core
import dupscan.infra.removal_history as removal_history
import dupscan.infra.session as session
import dupscan.ui.app as app_mod

_qapp = QApplication.instance() or QApplication([])


def make(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_short_read_or_wrong_expected_size_is_never_accepted(tmp_path):
    path = tmp_path / "short.bin"
    path.write_bytes(b"abc")
    with pytest.raises(OSError):
        core._hash_file(str(path), 10, True, threading.Event())


def test_cache_rejects_same_size_and_restored_mtime(tmp_path):
    tree, data_dir = tmp_path / "tree", tmp_path / "data"
    a, b = tree / "a.bin", tree / "b.bin"
    make(a, b"GOOD")
    make(b, b"GOOD")
    old = os.stat(a)
    with cache.HashCache.open(str(data_dir)) as hc:
        assert len(core.scan([str(tree)], cache=hc).file_groups) == 1

    a.write_bytes(b"EVIL")  # той самий розмір
    os.utime(a, ns=(old.st_atime_ns, old.st_mtime_ns))  # mtime навмисно відновлено
    with cache.HashCache.open(str(data_dir)) as hc:
        result = core.scan([str(tree)], cache=hc)
    assert result.file_groups == []


def test_zero_files_avoid_unstable_hash_identity_and_keep_folder_identity(
        tmp_path, monkeypatch):
    for name in ("A", "B"):
        make(tmp_path / name / "empty.bin", b"")
    real_hash = core._hash_file

    def no_empty_hash(path, size, *args, **kwargs):
        assert size != 0, "нульовий файл не треба відкривати для hashing"
        return real_hash(path, size, *args, **kwargs)

    monkeypatch.setattr(core, "_hash_file", no_empty_hash)
    result = core.scan([str(tmp_path)])
    assert result.errors == []
    assert any(g.size == 0 and len(g.paths) == 2 for g in result.file_groups)
    assert any({os.path.basename(p) for p in g.paths} == {"A", "B"}
               for g in result.dir_groups)

    os.symlink("empty.bin", tmp_path / "A/link")
    result = core.scan([str(tmp_path)])
    assert not any({os.path.basename(p) for p in g.paths} == {"A", "B"}
                   for g in result.dir_groups)


def test_import_rejects_path_outside_declared_directory(tmp_path):
    payload = {
        "version": 2,
        "created_ns": 1,
        "roots": ["/safe"],
        "state": {
            "file_meta": {"/outside/x": [1, 1, 1, 1, 1, 1]},
            "file_class": {"/outside/x": "u:1"},
            "class_size": {},
            "class_paths": {},
            "dir_files": {"/safe": ["/outside/x"]},
            "dir_children": {},
            "dir_links": {},
            "dir_ok": {"/safe": True},
            "ignored_pairs": [],
        },
    }
    src = tmp_path / "hostile.json"
    src.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        session.import_session(str(src), base_dir=str(tmp_path / "data"))


def test_merge_plan_rejects_injected_outside_path():
    result = core.ScanResult()
    outside = "/outside/secret.bin"
    result.file_meta[outside] = core.FileInfo(outside, 1, 1, 1)
    result.file_class[outside] = "u:1"
    result.dir_files["/src"] = [outside]
    result.dir_files["/dst"] = []
    with pytest.raises(ValueError):
        core.merge_plan(result, "/src", "/dst")


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("tree", "tree"),
        ("tree", "tree/child"),
        ("tree/child", "tree"),
    ],
)
def test_merge_plan_rejects_same_or_nested_roots(tmp_path, source, target):
    source_path = tmp_path / source
    target_path = tmp_path / target
    source_path.mkdir(parents=True, exist_ok=True)
    target_path.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="невкладеними"):
        core.merge_plan(core.ScanResult(), str(source_path), str(target_path))


def test_excluded_system_prefix_is_path_aware():
    assert core._excluded("/System/Library/item", "item")
    assert not core._excluded("/Systematic/archive/item", "item")


def test_last_moment_trash_verification_rejects_changed_survivor(
    tmp_path, monkeypatch
):
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    make(a, b"GOOD")
    make(b, b"GOOD")
    result = core.scan([str(tmp_path)])
    old = os.stat(b)
    b.write_bytes(b"EVIL")
    os.utime(b, ns=(old.st_atime_ns, old.st_mtime_ns))
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1]
    )
    errors = app_mod._verified_to_trash_files(result, [str(a)])
    assert errors
    assert trashed == []


def test_trash_identity_barrier_rejects_replacement_before_system_call(
        tmp_path, monkeypatch):
    victim = tmp_path / "victim.bin"
    replacement = tmp_path / "replacement.bin"
    make(victim, b"verified")
    make(replacement, b"different-object")
    expected = app_mod._trash_identity(os.lstat(victim))
    replacement_stat = os.lstat(replacement)
    real_lstat = app_mod.os.lstat
    calls = 0

    def changed_on_final_check(path):
        nonlocal calls
        if os.fspath(path) == str(victim):
            calls += 1
            if calls >= 2:
                return replacement_stat
        return real_lstat(path)

    trashed = []
    monkeypatch.setattr(app_mod.os, "lstat", changed_on_final_check)
    monkeypatch.setattr(send2trash, "send2trash", trashed.append)

    errors = app_mod.to_trash(
        [str(victim)], expected_identities={str(victim): expected})

    assert errors
    assert trashed == []
    assert victim.read_bytes() == b"verified"


def test_trash_history_records_identity_of_located_system_item(
    tmp_path,
    monkeypatch,
):
    victim = tmp_path / "victim.bin"
    fake_trash = tmp_path / ".Trash"
    data_dir = tmp_path / "data"
    fake_trash.mkdir()
    make(victim, b"verified")
    expected = app_mod._trash_identity(os.lstat(victim))
    real_expanduser = app_mod.os.path.expanduser

    def fake_expanduser(value):
        if value == "~/.Trash":
            return str(fake_trash)
        if value == "~":
            return str(tmp_path)
        return real_expanduser(value)

    def move_to_fake_trash(path):
        os.rename(path, fake_trash / os.path.basename(path))

    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(data_dir))
    monkeypatch.setattr(app_mod.os.path, "expanduser", fake_expanduser)
    monkeypatch.setattr(send2trash, "send2trash", move_to_fake_trash)

    assert app_mod.to_trash(
        [str(victim)],
        expected_identities={str(victim): expected},
    ) == []
    operations = removal_history.list_operations(
        base_dir=str(data_dir))
    recorded = operations[0]["items"][0]
    trashed_path = fake_trash / victim.name
    assert recorded["trashed_path"] == str(trashed_path)
    assert recorded["identity"] == list(
        app_mod._trash_identity(os.lstat(trashed_path)))


def test_fresh_directory_snapshot_detects_empty_file_and_link(tmp_path):
    for name in ("A", "B"):
        make(tmp_path / name / "same.bin", b"same")
        make(tmp_path / name / "empty.bin", b"")
    assert core.snapshot_directory(str(tmp_path / "A")) == core.snapshot_directory(
        str(tmp_path / "B")
    )
    os.symlink("same.bin", tmp_path / "A/link")
    assert core.snapshot_directory(str(tmp_path / "A")) != core.snapshot_directory(
        str(tmp_path / "B")
    )


def test_whole_directory_trash_aborts_if_tree_changes_during_verification(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    make(source / "same.bin", b"same")
    snapshots = iter([("before", 4, 1), ("after", 5, 2)])
    monkeypatch.setattr(
        app_mod.core, "snapshot_directory_state",
        lambda _path, **_kwargs: next(snapshots))
    monkeypatch.setattr(
        app_mod, "_verified_survivor", lambda *_args, **_kwargs: True)
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1])

    kind, count = app_mod._verify_then_trash_dir(
        core.ScanResult(), str(source))

    assert kind == "abort"
    assert count == 1
    assert trashed == []


def test_whole_directory_trash_passes_proven_root_identity(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    make(source / "same.bin", b"same")
    snapshot = ("same", 4, 1)
    monkeypatch.setattr(
        app_mod.core, "snapshot_directory_state",
        lambda _path, **_kwargs: snapshot)
    monkeypatch.setattr(
        app_mod, "_verified_survivor",
        lambda *_args, **_kwargs: os.lstat(source / "same.bin"))
    captured = {}

    def capture(paths, **kwargs):
        captured["paths"] = paths
        captured.update(kwargs)
        return []

    monkeypatch.setattr(app_mod, "to_trash", capture)

    kind, errors = app_mod._verify_then_trash_dir(
        core.ScanResult(), str(source))

    assert kind == "ok" and errors == []
    assert captured["paths"] == [str(source)]
    assert captured["expected_identities"][str(source)] == (
        app_mod._trash_identity(os.lstat(source)))


def test_problems_dialog_populates_large_error_list_in_pages():
    errors = [f"/volume/path/{index}: unreadable" for index in range(2_000)]
    dialog = app_mod.ProblemsDialog(None, errors)

    assert dialog.list.count() == dialog.PAGE_SIZE
    assert dialog.b_more.isEnabled()
    dialog.b_more.click()
    assert dialog.list.count() == dialog.PAGE_SIZE * 2
    assert "лишилось 1,000" in dialog.b_more.text()
    dialog.close()


def test_error_details_are_bounded_while_total_remains_exact(monkeypatch):
    monkeypatch.setattr(core, "MAX_ERROR_DETAILS", 3)
    result = core.ScanResult()
    for index in range(8):
        core._record_error(result, f"error-{index}")

    assert result.errors == ["error-0", "error-1", "error-2"]
    assert result.errors_total == 8
    assert app_mod._problem_count(result) == 8


def test_hardlink_entry_is_in_directory_manifest_but_not_file_duplicates(tmp_path):
    for name in ("A", "B"):
        make(tmp_path / name / "x.bin", b"same")
    os.link(tmp_path / "A/x.bin", tmp_path / "A/extra.bin")
    result = core.scan([str(tmp_path)])
    assert len(result.file_groups) == 1  # лише A/x та B/x, не A/extra
    assert len(result.file_groups[0].paths) == 2
    assert not any({os.path.basename(p) for p in group.paths} == {"A", "B"}
                   for group in result.dir_groups)


def test_nested_and_symlink_roots_visit_physical_tree_once(tmp_path):
    make(tmp_path / "root/sub/x.bin", b"x")
    os.symlink(tmp_path / "root", tmp_path / "alias")
    roots = [str(tmp_path / "root/sub"), str(tmp_path / "alias"),
             str(tmp_path / "root")]
    sequential = core.scan(roots, walk_threads=1)
    parallel = core.scan(roots, walk_threads=8)
    assert sequential.files_seen == parallel.files_seen == 1
    assert len(sequential.dir_children[str(tmp_path / "root")]) == 1
    assert len(parallel.dir_children[str(tmp_path / "root")]) == 1


def test_disconnected_external_volume_fails_closed(monkeypatch):
    root = "/Volumes/OfflineNAS/share"
    monkeypatch.setattr(core, "_normalize_roots", lambda roots, errors: [root])
    monkeypatch.setattr(core, "_is_external", lambda _path: True)
    monkeypatch.setattr(core.os.path, "isdir", lambda _path: True)

    def disconnected(_path):
        raise OSError(errno.ENOTCONN, "Socket is not connected", root)

    monkeypatch.setattr(core.os, "scandir", disconnected)

    result = core.scan([root])

    assert result.file_groups == []
    assert result.dir_groups == []
    assert result.sim_pairs == []
    assert result.errors and "not connected" in result.errors[0].lower()
    assert not core.directory_tree_complete(result, root)


def test_loaded_session_is_read_only_for_destructive_actions(tmp_path, monkeypatch):
    for name in ("a.bin", "b.bin"):
        make(tmp_path / "tree" / name, b"same")
    result = core.scan([str(tmp_path / "tree")])
    path = session.save_session(
        result, [str(tmp_path / "tree")], base_dir=str(tmp_path / "data"))
    loaded = session.load_session(path)
    assert loaded.live is False

    main = app_mod.Main()
    main.result = loaded
    main.m_files.set_groups(loaded.file_groups)
    main.m_files.checked.add(loaded.file_groups[0].paths[0])
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda *args, **_kwargs: messages.append(args[-1]))
    trashed = []
    monkeypatch.setattr(
        app_mod, "to_trash",
        lambda paths, **_kwargs: (trashed.extend(paths), [])[1])
    main.delete_checked(main.m_files)
    assert messages and "лише для перегляду" in messages[-1]
    assert trashed == []
    main.close()
