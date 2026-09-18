import json
import os
import stat
import sys

import blake3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.removal_history as history


def item(original, *, trashed=None, size=4, kind="file", status="trashed",
         digest=None):
    value = {
        "original_path": str(original),
        "size": size,
        "kind": kind,
        "status": status,
    }
    if digest is not None:
        value["digest"] = digest
    if trashed is not None:
        value["trashed_path"] = str(trashed)
        if os.path.lexists(trashed):
            value["identity"] = list(
                history._stat_identity(os.lstat(trashed)))
    return value


def test_append_list_get_update_are_atomic_and_detached(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    root.mkdir()
    first = history.append_operation(
        [item(root / "a.bin")],
        operation_id="1" * 32,
        created_ns=100,
        base_dir=str(base),
    )
    second = history.append_operation(
        [item(root / "b.bin", status="failed")],
        status="partial",
        errors=["permission denied"],
        operation_id="2" * 32,
        created_ns=200,
        base_dir=str(base),
    )

    assert [op["id"] for op in history.list_operations(base_dir=str(base))] == [
        second["id"],
        first["id"],
    ]
    first["items"][0]["status"] = "corrupted by caller"
    assert history.get_operation("1" * 32, base_dir=str(base))["items"][0]["status"] == "trashed"

    changed = history.update_operation(
        "2" * 32,
        status="failed",
        errors=["still unavailable"],
        item_updates={str(root / "b.bin"): {"error": "I/O error", "status": "failed"}},
        base_dir=str(base),
    )
    assert changed["status"] == "failed"
    assert changed["items"][0]["error"] == "I/O error"
    store = history.history_path(str(base))
    assert stat.S_IMODE(os.stat(store).st_mode) == 0o600
    assert not list((base / "removal_history").glob("*.tmp"))


def test_history_prunes_oldest_operations_to_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "MAX_OPERATIONS", 2)
    root = tmp_path / "root"
    root.mkdir()
    for number in range(3):
        history.append_operation(
            [item(root / f"{number}.bin")],
            operation_id=f"{number + 1:032x}",
            created_ns=number + 1,
            base_dir=str(tmp_path / "data"),
        )
    operations = history.list_operations(base_dir=str(tmp_path / "data"))
    assert [op["created_ns"] for op in operations] == [3, 2]


@pytest.mark.parametrize(
    "bad_item",
    [
        {"original_path": "relative", "size": 1, "kind": "file", "status": "trashed"},
        {"original_path": "/tmp/a/../b", "size": 1, "kind": "file", "status": "trashed"},
        {"original_path": "/tmp/a\0b", "size": 1, "kind": "file", "status": "trashed"},
        {"original_path": "/tmp/a", "size": -1, "kind": "file", "status": "trashed"},
        {"original_path": "/tmp/a", "size": 1, "kind": "device", "status": "trashed"},
        {"original_path": "/tmp/a", "size": 1, "kind": "file", "status": "invented"},
        {
            "original_path": "/tmp/a",
            "size": 1,
            "kind": "file",
            "status": "trashed",
            "digest": "not-a-digest",
        },
    ],
)
def test_append_rejects_hostile_or_malformed_items(tmp_path, bad_item):
    with pytest.raises(history.HistoryValidationError):
        history.append_operation([bad_item], base_dir=str(tmp_path / "data"))
    assert not os.path.exists(history.history_path(str(tmp_path / "data")))


def test_corrupt_or_symlinked_store_is_never_followed(tmp_path):
    base = tmp_path / "data"
    store = history.history_path(str(base))
    os.makedirs(os.path.dirname(store))
    with open(store, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "operations": [{"hostile": True}]}, stream)
    with pytest.raises(history.HistoryValidationError):
        history.list_operations(base_dir=str(base))

    os.unlink(store)
    target = tmp_path / "do-not-touch"
    target.write_text("sentinel")
    os.symlink(target, store)
    with pytest.raises((history.HistoryValidationError, OSError)):
        history.append_operation([item(tmp_path / "x")], base_dir=str(base))
    assert target.read_text() == "sentinel"


def test_store_rejects_duplicate_json_fields(tmp_path):
    base = tmp_path / "data"
    store = history.history_path(str(base))
    os.makedirs(os.path.dirname(store))
    with open(store, "w", encoding="utf-8") as stream:
        stream.write('{"version":1,"version":1,"operations":[]}')
    with pytest.raises(history.HistoryValidationError, match="duplicate JSON"):
        history.list_operations(base_dir=str(base))


def test_failed_atomic_commit_preserves_previous_history(tmp_path, monkeypatch):
    base = tmp_path / "data"
    root = tmp_path / "root"
    root.mkdir()
    history.append_operation([item(root / "a")], base_dir=str(base))
    store = history.history_path(str(base))
    before = open(store, "rb").read()
    original_replace = history.os.replace

    def fail_history_replace(source, destination):
        if destination == store:
            raise OSError("simulated disk failure")
        return original_replace(source, destination)

    monkeypatch.setattr(history.os, "replace", fail_history_replace)
    with pytest.raises(OSError, match="simulated"):
        history.append_operation([item(root / "b")], base_dir=str(base))
    assert open(store, "rb").read() == before
    assert not list((base / "removal_history").glob("*.tmp"))


def test_restore_item_uses_explicit_path_and_updates_history(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    original = root / "note.txt"
    trashed = trash / "note.txt"
    trashed.write_bytes(b"data")
    operation = history.append_operation(
        [item(original, trashed=trashed)], base_dir=str(base)
    )

    restored = history.restore_item(
        operation["id"], str(original), allowed_roots=[str(root)], base_dir=str(base)
    )
    assert restored["restored_path"] == str(original)
    assert restored["collision"] is False
    assert original.read_bytes() == b"data"
    assert not trashed.exists()
    saved = history.get_operation(operation["id"], base_dir=str(base))
    assert saved["status"] == "restored"
    assert saved["items"][0]["status"] == "restored"
    assert saved["items"][0]["restored_path"] == str(original)


def test_restore_collision_never_overwrites_and_uses_safe_suffix(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    original = root / "photo.jpg"
    original.write_bytes(b"keep")
    (root / "photo (restored 1).jpg").write_bytes(b"keep-too")
    trashed = trash / "photo.jpg"
    trashed.write_bytes(b"data")
    operation = history.append_operation(
        [item(original, trashed=trashed)], base_dir=str(base)
    )

    restored = history.restore_item(
        operation["id"], str(original), allowed_roots=[str(root)], base_dir=str(base)
    )
    destination = root / "photo (restored 2).jpg"
    assert restored == {
        "operation_id": operation["id"],
        "original_path": str(original),
        "restored_path": str(destination),
        "collision": True,
    }
    assert original.read_bytes() == b"keep"
    assert (root / "photo (restored 1).jpg").read_bytes() == b"keep-too"
    assert destination.read_bytes() == b"data"


def test_restore_verifies_recorded_content_digest(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    original = root / "proof.bin"
    source = trash / "proof.bin"
    source.write_bytes(b"evil")
    expected = blake3.blake3(b"safe").hexdigest()
    operation = history.append_operation(
        [item(original, trashed=source, digest=expected)], base_dir=str(base))

    with pytest.raises(history.HistoryValidationError, match="no longer matches"):
        history.restore_item(
            operation["id"], str(original), allowed_roots=[str(root)],
            base_dir=str(base))
    assert source.exists()
    assert not original.exists()


def test_restore_refuses_missing_trash_path_escape_and_changed_source(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    other = tmp_path / "other"
    trash = tmp_path / "trash"
    root.mkdir()
    other.mkdir()
    trash.mkdir()
    missing_path_op = history.append_operation(
        [item(root / "missing.bin")], base_dir=str(base)
    )
    with pytest.raises(history.HistoryValidationError, match="trashed_path"):
        history.restore_item(
            missing_path_op["id"],
            str(root / "missing.bin"),
            allowed_roots=[str(root)],
            base_dir=str(base),
        )

    source = trash / "changed.bin"
    source.write_bytes(b"longer")
    changed_op = history.append_operation(
        [item(other / "changed.bin", trashed=source, size=4)], base_dir=str(base)
    )
    with pytest.raises(history.HistoryValidationError, match="size changed"):
        history.restore_item(
            changed_op["id"],
            str(other / "changed.bin"),
            allowed_roots=[str(other)],
            base_dir=str(base),
        )
    assert source.exists()

    history.update_operation(
        changed_op["id"],
        item_updates={str(other / "changed.bin"): {"size": len(b"longer")}},
        base_dir=str(base),
    )
    with pytest.raises(history.HistoryValidationError, match="escapes"):
        history.restore_item(
            changed_op["id"],
            str(other / "changed.bin"),
            allowed_roots=[str(root)],
            base_dir=str(base),
        )
    assert source.exists()


def test_restore_refuses_a_trash_path_equal_to_the_original(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    root.mkdir()
    original = root / "live.bin"
    original.write_bytes(b"data")
    operation = history.append_operation(
        [item(original, trashed=original)], base_dir=str(base)
    )
    with pytest.raises(history.HistoryValidationError, match="must differ"):
        history.restore_item(
            operation["id"], str(original), allowed_roots=[str(root)], base_dir=str(base)
        )
    assert original.read_bytes() == b"data"


def test_restore_operation_preflights_all_explicit_trash_paths(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    source = trash / "a"
    source.write_bytes(b"data")
    operation = history.append_operation(
        [item(root / "a", trashed=source), item(root / "b")], base_dir=str(base)
    )
    with pytest.raises(history.HistoryValidationError, match="explicit"):
        history.restore_operation(
            operation["id"], allowed_roots=[str(root)], base_dir=str(base)
        )
    assert source.exists()
    assert not (root / "a").exists()


def test_restore_operation_checks_every_source_before_first_move(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    source_a = trash / "a"
    source_a.write_bytes(b"data-a")
    missing_b = trash / "missing-b"
    operation = history.append_operation(
        [
            item(
                root / "a",
                trashed=source_a,
                size=len(b"data-a"),
                digest=blake3.blake3(b"data-a").hexdigest(),
            ),
            item(
                root / "b",
                trashed=missing_b,
                size=len(b"data-b"),
                digest=blake3.blake3(b"data-b").hexdigest(),
            ),
        ],
        base_dir=str(base),
    )

    with pytest.raises(FileNotFoundError):
        history.restore_operation(
            operation["id"],
            allowed_roots=[str(root)],
            base_dir=str(base),
        )
    assert source_a.read_bytes() == b"data-a"
    assert not (root / "a").exists()


def test_legacy_directory_without_trash_identity_uses_finder_fallback(tmp_path):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    source = trash / "folder"
    source.mkdir()
    legacy = item(
        root / "folder",
        trashed=source,
        kind="directory",
        size=source.stat().st_size,
    )
    legacy.pop("identity")
    operation = history.append_operation([legacy], base_dir=str(base))

    with pytest.raises(history.HistoryValidationError, match="Finder"):
        history.restore_item(
            operation["id"],
            str(root / "folder"),
            allowed_roots=[str(root)],
            base_dir=str(base),
        )
    assert source.is_dir()
    assert not (root / "folder").exists()


def test_restore_capability_requires_path_and_identity_or_file_digest():
    base = {
        "trashed_path": "/tmp/Trash/item",
        "kind": "directory",
    }
    assert not history.can_restore_automatically(base)
    assert history.can_restore_automatically({
        **base,
        "identity": [1, 2, 3, 4, 5, 6],
    })
    assert history.can_restore_automatically({
        **base,
        "kind": "file",
        "digest": "a" * 64,
    })
    assert not history.can_restore_automatically({
        "kind": "file",
        "digest": "a" * 64,
    })


def test_restore_rejects_source_replacement_after_digest_proof(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "data"
    root = tmp_path / "root"
    trash = tmp_path / "trash"
    root.mkdir()
    trash.mkdir()
    original = root / "proof.bin"
    source = trash / "proof.bin"
    source.write_bytes(b"safe")
    digest = blake3.blake3(b"safe").hexdigest()
    operation = history.append_operation(
        [item(original, trashed=source, digest=digest)],
        base_dir=str(base),
    )
    parked = trash / "verified-original.bin"
    real_collision = history._collision_destination_name

    def replace_source(parent_fd, name):
        os.rename(source, parked)
        source.write_bytes(b"evil")
        return real_collision(parent_fd, name)

    monkeypatch.setattr(
        history,
        "_collision_destination_name",
        replace_source,
    )
    with pytest.raises(
        history.HistoryValidationError,
        match="changed after verification",
    ):
        history.restore_item(
            operation["id"],
            str(original),
            allowed_roots=[str(root)],
            base_dir=str(base),
        )
    assert parked.read_bytes() == b"safe"
    assert source.read_bytes() == b"evil"
    assert not original.exists()


def test_restore_rejects_destination_parent_replacement_without_outside_write(
    tmp_path,
    monkeypatch,
):
    base = tmp_path / "data"
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    trash = tmp_path / "trash"
    root.mkdir()
    outside.mkdir()
    trash.mkdir()
    original = root / "note.txt"
    source = trash / "note.txt"
    source.write_bytes(b"safe")
    operation = history.append_operation(
        [
            item(
                original,
                trashed=source,
                digest=blake3.blake3(b"safe").hexdigest(),
            )
        ],
        base_dir=str(base),
    )
    parked_root = tmp_path / "verified-root"
    real_collision = history._collision_destination_name

    def replace_parent(parent_fd, name):
        os.rename(root, parked_root)
        os.symlink(outside, root)
        return real_collision(parent_fd, name)

    monkeypatch.setattr(
        history,
        "_collision_destination_name",
        replace_parent,
    )
    with pytest.raises(
        history.HistoryValidationError,
        match="path changed",
    ):
        history.restore_item(
            operation["id"],
            str(original),
            allowed_roots=[str(root)],
            base_dir=str(base),
        )
    assert source.read_bytes() == b"safe"
    assert not (outside / "note.txt").exists()
    assert not (parked_root / "note.txt").exists()
