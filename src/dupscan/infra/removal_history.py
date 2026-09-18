"""Bounded, validated removal history and conservative restore helpers.

The module deliberately has no dependency on Qt, ``core`` or Send2Trash.  A
caller may record the path reported by its trash backend as ``trashed_path``;
entries without that explicit path remain useful audit records but cannot be
restored automatically.
"""

from __future__ import annotations

import copy
import errno
import fcntl
import io
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import blake3


VERSION = 1
MAX_OPERATIONS = 500
MAX_ITEMS_PER_OPERATION = 10_000
MAX_TOTAL_ITEMS = 50_000
MAX_HISTORY_BYTES = 16 * 1024 * 1024
MAX_STRING = 32_768
MAX_ERROR = 4_096
MAX_ERRORS = 100

_OP_STATUSES = {"completed", "partial", "failed", "restored"}
_ITEM_STATUSES = {"pending", "trashed", "failed", "restored", "missing", "skipped"}
_KINDS = {"file", "directory", "symlink"}
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
class HistoryValidationError(ValueError):
    """The on-disk history or supplied entry failed strict validation."""


def _default_base_dir() -> str:
    return os.environ.get("DUPSCAN_DATA_DIR") or os.path.expanduser(
        "~/Library/Application Support/DupScan"
    )


def history_path(base_dir: str | None = None) -> str:
    """Return the JSON store path (primarily useful for diagnostics/tests)."""
    base = base_dir if base_dir is not None else _default_base_dir()
    return os.path.join(base, "removal_history", "history.json")


def _safe_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise HistoryValidationError(f"invalid path in {label}")
    if len(value) > MAX_STRING or not os.path.isabs(value):
        raise HistoryValidationError(f"unsafe path in {label}")
    if os.path.normpath(value) != value or not os.path.basename(value):
        raise HistoryValidationError(f"path is not normalized in {label}")
    return value


def _safe_root(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise HistoryValidationError(f"invalid path in {label}")
    if len(value) > MAX_STRING or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise HistoryValidationError(f"unsafe path in {label}")
    return value


def _safe_text(value: object, label: str, maximum: int = MAX_ERROR) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > maximum:
        raise HistoryValidationError(f"invalid text in {label}")
    return value


def _error_list(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise HistoryValidationError("errors must be a collection of messages")
    result: list[str] = []
    for value in values:
        if len(result) >= MAX_ERRORS:
            raise HistoryValidationError("too many operation errors")
        result.append(_safe_text(value, "operation error"))
    return result


def _safe_int(value: object, label: str, *, positive: bool = False) -> int:
    # bool is intentionally rejected even though it subclasses int.
    if type(value) is not int or value < (1 if positive else 0) or value > 2**63 - 1:
        raise HistoryValidationError(f"invalid integer in {label}")
    return value


def _validate_item(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HistoryValidationError("history item must be an object")
    allowed = {
        "original_path",
        "trashed_path",
        "restored_path",
        "restored_ns",
        "size",
        "kind",
        "digest",
        "identity",
        "status",
        "error",
    }
    if set(value) - allowed:
        raise HistoryValidationError("history item has unknown fields")
    required = {"original_path", "size", "kind", "status"}
    if not required <= set(value):
        raise HistoryValidationError("history item is incomplete")

    item: dict[str, Any] = {
        "original_path": _safe_path(value["original_path"], "original_path"),
        "size": _safe_int(value["size"], "size"),
        "kind": value["kind"],
        "status": value["status"],
    }
    if item["kind"] not in _KINDS:
        raise HistoryValidationError("invalid item kind")
    if item["status"] not in _ITEM_STATUSES:
        raise HistoryValidationError("invalid item status")

    for key in ("trashed_path", "restored_path"):
        if key in value:
            item[key] = _safe_path(value[key], key)
    if "restored_ns" in value:
        item["restored_ns"] = _safe_int(value["restored_ns"], "restored_ns", positive=True)
    if "digest" in value:
        digest = value["digest"]
        if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
            raise HistoryValidationError("invalid digest")
        item["digest"] = digest
    if "identity" in value:
        identity = value["identity"]
        if (
            not isinstance(identity, list)
            or len(identity) != 6
            or any(type(part) is not int or part < 0 for part in identity)
        ):
            raise HistoryValidationError("invalid Trash identity")
        item["identity"] = [
            _safe_int(part, "Trash identity") for part in identity
        ]
    if "error" in value:
        item["error"] = _safe_text(value["error"], "item error")
    restored_fields = {"restored_path", "restored_ns"}
    if item["status"] == "restored" and not restored_fields <= set(item):
        raise HistoryValidationError("restored item is missing restore metadata")
    if item["status"] != "restored" and restored_fields & set(item):
        raise HistoryValidationError("non-restored item contains restore metadata")
    return item


def _validate_operation(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HistoryValidationError("history operation must be an object")
    allowed = {"id", "created_ns", "updated_ns", "status", "errors", "items"}
    if set(value) - allowed or not allowed <= set(value):
        raise HistoryValidationError("history operation has invalid fields")
    operation_id = value["id"]
    if not isinstance(operation_id, str) or not _ID_RE.fullmatch(operation_id):
        raise HistoryValidationError("invalid operation id")
    created_ns = _safe_int(value["created_ns"], "created_ns", positive=True)
    updated_ns = _safe_int(value["updated_ns"], "updated_ns", positive=True)
    if updated_ns < created_ns:
        raise HistoryValidationError("updated_ns precedes created_ns")
    status_value = value["status"]
    if status_value not in _OP_STATUSES:
        raise HistoryValidationError("invalid operation status")
    errors_value = value["errors"]
    if not isinstance(errors_value, list) or len(errors_value) > MAX_ERRORS:
        raise HistoryValidationError("invalid operation errors")
    errors = [_safe_text(item, "operation error") for item in errors_value]
    items_value = value["items"]
    if not isinstance(items_value, list) or not items_value:
        raise HistoryValidationError("operation must contain items")
    if len(items_value) > MAX_ITEMS_PER_OPERATION:
        raise HistoryValidationError("operation contains too many items")
    items = [_validate_item(item) for item in items_value]
    originals = [item["original_path"] for item in items]
    if len(set(originals)) != len(originals):
        raise HistoryValidationError("operation contains duplicate original paths")
    if status_value == "restored" and not all(
        item["status"] in {"restored", "skipped"} for item in items
    ):
        raise HistoryValidationError("restored operation contains pending items")
    return {
        "id": operation_id,
        "created_ns": created_ns,
        "updated_ns": updated_ns,
        "status": status_value,
        "errors": errors,
        "items": items,
    }


def _validate_store(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "operations"}:
        raise HistoryValidationError("invalid removal history document")
    if value["version"] != VERSION or not isinstance(value["operations"], list):
        raise HistoryValidationError("unsupported removal history version")
    if len(value["operations"]) > MAX_OPERATIONS:
        raise HistoryValidationError("removal history has too many operations")
    operations = [_validate_operation(op) for op in value["operations"]]
    if len({op["id"] for op in operations}) != len(operations):
        raise HistoryValidationError("duplicate operation id")
    if sum(len(op["items"]) for op in operations) > MAX_TOTAL_ITEMS:
        raise HistoryValidationError("removal history has too many items")
    return {"version": VERSION, "operations": operations}


def _ensure_store_dir(path: str) -> str:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise HistoryValidationError("removal history directory is unsafe")
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _open_regular(path: str, flags: int, mode: int = 0o600) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags | nofollow, mode)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise HistoryValidationError("removal history path is not a regular file")
    return fd


@contextmanager
def _locked(base_dir: str | None):
    path = history_path(base_dir)
    directory = _ensure_store_dir(path)
    lock_path = os.path.join(directory, ".lock")
    fd = _open_regular(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path: str) -> dict[str, Any]:
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return {"version": VERSION, "operations": []}
    try:
        info = os.fstat(fd)
        if info.st_size > MAX_HISTORY_BYTES:
            raise HistoryValidationError("removal history is too large")
        with io.TextIOWrapper(io.FileIO(fd, mode="r", closefd=False), encoding="utf-8") as stream:
            try:
                value = json.load(stream, object_pairs_hook=_object_without_duplicates)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                raise HistoryValidationError("removal history JSON is corrupted") from exc
    finally:
        os.close(fd)
    return _validate_store(value)


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise HistoryValidationError(f"duplicate JSON field: {key}")
        value[key] = item
    return value


def _encoded(store: dict[str, Any]) -> bytes:
    return json.dumps(
        store, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _bound(store: dict[str, Any]) -> bytes:
    operations = store["operations"]
    while len(operations) > MAX_OPERATIONS:
        del operations[0]
    while sum(len(op["items"]) for op in operations) > MAX_TOTAL_ITEMS and len(operations) > 1:
        del operations[0]
    if sum(len(op["items"]) for op in operations) > MAX_TOTAL_ITEMS:
        raise HistoryValidationError("one removal operation exceeds the item limit")
    payload = _encoded(store)
    while len(payload) > MAX_HISTORY_BYTES and len(operations) > 1:
        del operations[0]
        payload = _encoded(store)
    if len(payload) > MAX_HISTORY_BYTES:
        raise HistoryValidationError("one removal operation exceeds the history size limit")
    return payload


def _write(path: str, store: dict[str, Any]) -> None:
    payload = _bound(store)
    directory = os.path.dirname(path)
    if os.path.lexists(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise HistoryValidationError("refusing to replace unsafe history path")
    temp_path = os.path.join(directory, f".history-{uuid.uuid4().hex}.tmp")
    fd = _open_regular(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "short history write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Atomic replacement already succeeded.  Directory fsync is an
        # extra durability barrier unsupported by some filesystems.
        pass


def append_operation(
    items: Iterable[Mapping[str, Any]],
    *,
    status: str = "completed",
    errors: Iterable[str] = (),
    operation_id: str | None = None,
    created_ns: int | None = None,
    base_dir: str | None = None,
) -> dict[str, Any]:
    """Validate and atomically append one removal operation."""
    now = time.time_ns() if created_ns is None else created_ns
    raw_items: list[dict[str, Any]] = []
    for raw in items:
        if len(raw_items) >= MAX_ITEMS_PER_OPERATION:
            raise HistoryValidationError("operation contains too many items")
        item = dict(raw)
        item.setdefault("status", "trashed")
        raw_items.append(item)
    operation = _validate_operation(
        {
            "id": operation_id or uuid.uuid4().hex,
            "created_ns": now,
            "updated_ns": now,
            "status": status,
            "errors": _error_list(errors),
            "items": raw_items,
        }
    )
    with _locked(base_dir) as path:
        store = _read(path)
        if any(op["id"] == operation["id"] for op in store["operations"]):
            raise HistoryValidationError("operation id already exists")
        store["operations"].append(operation)
        _write(path, store)
    return copy.deepcopy(operation)


def list_operations(
    *, base_dir: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return newest operations first.  Returned dictionaries are detached copies."""
    if limit is not None and (type(limit) is not int or limit < 0 or limit > MAX_OPERATIONS):
        raise ValueError("invalid history limit")
    with _locked(base_dir) as path:
        operations = list(reversed(_read(path)["operations"]))
    if limit is not None:
        operations = operations[:limit]
    return copy.deepcopy(operations)


def get_operation(operation_id: str, *, base_dir: str | None = None) -> dict[str, Any] | None:
    if not isinstance(operation_id, str) or not _ID_RE.fullmatch(operation_id):
        raise HistoryValidationError("invalid operation id")
    with _locked(base_dir) as path:
        for operation in _read(path)["operations"]:
            if operation["id"] == operation_id:
                return copy.deepcopy(operation)
    return None


def can_restore_automatically(item: Mapping[str, Any]) -> bool:
    """Whether a journal row carries enough proof for conservative restore."""
    return bool(
        item.get("trashed_path")
        and (
            item.get("identity")
            or (item.get("kind") == "file" and item.get("digest"))
        )
    )


def update_operation(
    operation_id: str,
    *,
    status: str | None = None,
    errors: Iterable[str] | None = None,
    item_updates: Mapping[str, Mapping[str, Any]] | None = None,
    base_dir: str | None = None,
) -> dict[str, Any]:
    """Atomically update status/errors and selected items keyed by original path."""
    if not isinstance(operation_id, str) or not _ID_RE.fullmatch(operation_id):
        raise HistoryValidationError("invalid operation id")
    updates = {} if item_updates is None else dict(item_updates)
    allowed_item_changes = {
        "trashed_path", "restored_path", "restored_ns", "size", "kind",
        "digest", "identity", "status", "error",
    }
    with _locked(base_dir) as path:
        store = _read(path)
        operation = next((op for op in store["operations"] if op["id"] == operation_id), None)
        if operation is None:
            raise KeyError(operation_id)
        if status is not None:
            operation["status"] = status
        if errors is not None:
            operation["errors"] = _error_list(errors)
        known = {item["original_path"]: item for item in operation["items"]}
        for original, changes_value in updates.items():
            if original not in known:
                raise KeyError(original)
            if not isinstance(changes_value, Mapping):
                raise HistoryValidationError("item update must be an object")
            changes = dict(changes_value)
            if set(changes) - allowed_item_changes:
                raise HistoryValidationError("item update has unknown fields")
            candidate = dict(known[original])
            for key, value in changes.items():
                nullable = {
                    "trashed_path", "restored_path", "restored_ns", "digest", "error"
                }
                if value is None and key in nullable:
                    candidate.pop(key, None)
                else:
                    candidate[key] = value
            known[original].clear()
            known[original].update(_validate_item(candidate))
        operation["updated_ns"] = max(time.time_ns(), operation["created_ns"])
        validated = _validate_operation(operation)
        operation.clear()
        operation.update(validated)
        _write(path, store)
        return copy.deepcopy(operation)


def _open_absolute_directory(path: str) -> int:
    """Open every absolute directory component without following symlinks."""
    path = os.path.normpath(os.path.abspath(path))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    current = os.open(os.sep, flags)
    try:
        for part in (part for part in path.split(os.sep) if part):
            following = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = following
        info = os.fstat(current)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise HistoryValidationError("restore parent is not a real directory")
        return current
    except Exception:
        os.close(current)
        raise


def _open_destination_parent(
    original_path: str,
    allowed_roots: Sequence[str],
) -> tuple[int, str]:
    """Return a pinned parent FD contained lexically under a trusted root."""
    if not allowed_roots:
        raise HistoryValidationError(
            "restore requires at least one allowed destination root")
    parent = os.path.dirname(original_path)
    candidates: list[str] = []
    for raw in allowed_roots:
        root = _safe_root(raw, "allowed_roots")
        try:
            if os.path.commonpath((root, parent)) == root:
                candidates.append(root)
        except ValueError:
            continue
    if not candidates:
        raise HistoryValidationError("restore destination escapes allowed roots")
    root = max(candidates, key=len)
    current = _open_absolute_directory(root)
    try:
        relative = os.path.relpath(parent, root)
        if relative not in ("", "."):
            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for part in relative.split(os.sep):
                if part in ("", ".", ".."):
                    raise HistoryValidationError(
                        "unsafe restore destination component")
                following = os.open(part, flags, dir_fd=current)
                os.close(current)
                current = following
        return current, parent
    except Exception:
        os.close(current)
        raise


def _directory_path_matches_fd(path: str, descriptor: int) -> bool:
    try:
        path_info = os.lstat(path)
        pinned = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISDIR(path_info.st_mode)
        and not stat.S_ISLNK(path_info.st_mode)
        and (path_info.st_dev, path_info.st_ino)
        == (pinned.st_dev, pinned.st_ino)
    )


def _entry_stat(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _collision_destination_name(
    parent_fd: int,
    original_name: str,
) -> str:
    try:
        _entry_stat(parent_fd, original_name)
    except FileNotFoundError:
        return original_name
    stem, extension = os.path.splitext(original_name)
    for number in range(1, 10_001):
        candidate = f"{stem} (restored {number}){extension}"
        try:
            _entry_stat(parent_fd, candidate)
        except FileNotFoundError:
            return candidate
    raise FileExistsError("no collision-free restore destination")


def _stat_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(stat.S_IFMT(info.st_mode)),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _renameat_noreplace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    """Atomic no-replace rename bound to already verified directory FDs."""
    import ctypes

    source_b = os.fsencode(source_name)
    destination_b = os.fsencode(destination_name)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        if function(
            source_fd,
            source_b,
            destination_fd,
            destination_b,
            0x00000004,  # RENAME_EXCL
        ) == 0:
            return
        error = ctypes.get_errno()
        if error not in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            raise OSError(error, os.strerror(error), destination_name)
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        if function(
            source_fd,
            source_b,
            destination_fd,
            destination_b,
            1,  # RENAME_NOREPLACE
        ) == 0:
            return
        error = ctypes.get_errno()
        if error not in {errno.ENOSYS, errno.ENOTSUP, errno.EINVAL}:
            raise OSError(error, os.strerror(error), destination_name)

    source_info = _entry_stat(source_fd, source_name)
    reserved_info: os.stat_result
    if stat.S_ISDIR(source_info.st_mode):
        os.mkdir(destination_name, 0o700, dir_fd=destination_fd)
        reserved_info = _entry_stat(destination_fd, destination_name)
    else:
        fd = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=destination_fd,
        )
        try:
            reserved_info = os.fstat(fd)
        finally:
            os.close(fd)
    try:
        current = _entry_stat(destination_fd, destination_name)
        if (
            current.st_dev,
            current.st_ino,
        ) != (reserved_info.st_dev, reserved_info.st_ino):
            raise FileExistsError("restore destination reservation changed")
        os.replace(
            source_name,
            destination_name,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
        )
    except Exception:
        try:
            if stat.S_ISDIR(reserved_info.st_mode):
                os.rmdir(destination_name, dir_fd=destination_fd)
            else:
                os.unlink(destination_name, dir_fd=destination_fd)
        except OSError:
            pass
        raise


def _kind_matches_stat(info: os.stat_result, kind: str) -> bool:
    return {
        "file": stat.S_ISREG(info.st_mode),
        "directory": stat.S_ISDIR(info.st_mode),
        "symlink": stat.S_ISLNK(info.st_mode),
    }[kind]


def _verified_digest_at(
    parent_fd: int,
    name: str,
    expected_size: int,
) -> tuple[str, os.stat_result]:
    """Hash one regular Trash item through a stable file descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(
        name,
        os.O_RDONLY | nofollow,
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise HistoryValidationError("trashed file identity changed")
        digest = blake3.blake3()
        remaining = expected_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise HistoryValidationError("trashed file was truncated while reading")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
             before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                 after.st_ctime_ns)):
            raise HistoryValidationError("trashed file changed while reading")
        return digest.hexdigest(), after
    finally:
        os.close(descriptor)


def _preflight_restore_record(
    item: Mapping[str, Any],
    *,
    allowed_roots: Sequence[str],
) -> None:
    """Read-only proof used before a multi-item restore starts mutating."""
    original = _safe_path(item["original_path"], "original_path")
    source_value = item.get("trashed_path")
    if source_value is None:
        raise HistoryValidationError(
            "all restored items require explicit trashed_path values")
    source = _safe_path(source_value, "trashed_path")
    if source == original:
        raise HistoryValidationError(
            "trashed_path must differ from original_path")
    source_parent_fd = -1
    destination_parent_fd = -1
    try:
        source_parent = os.path.dirname(source)
        source_name = os.path.basename(source)
        source_parent_fd = _open_absolute_directory(source_parent)
        source_info = _entry_stat(source_parent_fd, source_name)
        if not _kind_matches_stat(source_info, item["kind"]):
            raise HistoryValidationError("trashed item kind changed")
        if item["kind"] == "file" and source_info.st_size != item["size"]:
            raise HistoryValidationError("trashed file size changed")
        recorded_identity = item.get("identity")
        if recorded_identity is not None:
            if _stat_identity(source_info) != tuple(recorded_identity):
                raise HistoryValidationError(
                    "trashed item identity no longer matches history")
        elif item["kind"] != "file" or not item.get("digest"):
            raise HistoryValidationError(
                "legacy Trash record has no identity proof; "
                "restore it through Finder")
        if item["kind"] == "file" and item.get("digest"):
            digest, verified_info = _verified_digest_at(
                source_parent_fd,
                source_name,
                item["size"],
            )
            if digest != item["digest"]:
                raise HistoryValidationError(
                    "trashed file content no longer matches history")
            if (
                recorded_identity is not None
                and _stat_identity(verified_info)
                != tuple(recorded_identity)
            ):
                raise HistoryValidationError(
                    "trashed file identity changed while reading")
        destination_parent_fd, parent = _open_destination_parent(
            original,
            allowed_roots,
        )
        if source_info.st_dev != os.fstat(destination_parent_fd).st_dev:
            raise OSError(
                errno.EXDEV,
                "restore requires an atomic same-device rename",
                source,
            )
        if (
            not _directory_path_matches_fd(
                source_parent,
                source_parent_fd,
            )
            or not _directory_path_matches_fd(
                parent,
                destination_parent_fd,
            )
        ):
            raise HistoryValidationError(
                "restore path changed during batch preflight")
    finally:
        if destination_parent_fd >= 0:
            os.close(destination_parent_fd)
        if source_parent_fd >= 0:
            os.close(source_parent_fd)


def restore_item(
    operation_id: str,
    original_path: str,
    *,
    allowed_roots: Sequence[str],
    base_dir: str | None = None,
) -> dict[str, Any]:
    """Restore one recorded item by an exclusive same-device rename.

    A collision at the original name produces ``"name (restored N).ext"``.
    The caller must provide roots that are currently trusted destinations.
    """
    if not isinstance(operation_id, str) or not _ID_RE.fullmatch(operation_id):
        raise HistoryValidationError("invalid operation id")
    original_path = _safe_path(original_path, "original_path")
    with _locked(base_dir) as path:
        store = _read(path)
        operation = next(
            (
                candidate
                for candidate in store["operations"]
                if candidate["id"] == operation_id
            ),
            None,
        )
        if operation is None:
            raise KeyError(operation_id)
        item = next(
            (
                candidate
                for candidate in operation["items"]
                if candidate["original_path"] == original_path
            ),
            None,
        )
        if item is None:
            raise KeyError(original_path)
        source = item.get("trashed_path")
        if source is None:
            raise HistoryValidationError("item has no explicit trashed_path")
        source = _safe_path(source, "trashed_path")
        if source == original_path:
            raise HistoryValidationError("trashed_path must differ from original_path")
        source_parent = os.path.dirname(source)
        source_name = os.path.basename(source)
        source_parent_fd = -1
        destination_parent_fd = -1
        destination_name = ""
        destination = ""
        try:
            source_parent_fd = _open_absolute_directory(source_parent)
            source_info = _entry_stat(source_parent_fd, source_name)
            if not _kind_matches_stat(source_info, item["kind"]):
                raise HistoryValidationError("trashed item kind changed")
            if item["kind"] == "file" and source_info.st_size != item["size"]:
                raise HistoryValidationError("trashed file size changed")
            destination_parent_fd, parent = _open_destination_parent(
                original_path,
                allowed_roots,
            )
            if source_info.st_dev != os.fstat(destination_parent_fd).st_dev:
                raise OSError(
                    errno.EXDEV,
                    "restore requires an atomic same-device rename",
                    source,
                )

            recorded_identity = item.get("identity")
            if recorded_identity is not None:
                if _stat_identity(source_info) != tuple(recorded_identity):
                    raise HistoryValidationError(
                        "trashed item identity no longer matches history")
            elif item["kind"] != "file" or not item.get("digest"):
                raise HistoryValidationError(
                    "legacy Trash record has no identity proof; "
                    "restore it through Finder")

            if item["kind"] == "file" and item.get("digest"):
                digest, source_info = _verified_digest_at(
                    source_parent_fd,
                    source_name,
                    item["size"],
                )
                if digest != item["digest"]:
                    raise HistoryValidationError(
                        "trashed file content no longer matches history")
                if (
                    recorded_identity is not None
                    and _stat_identity(source_info)
                    != tuple(recorded_identity)
                ):
                    raise HistoryValidationError(
                        "trashed file identity changed while reading")

            expected_source_identity = _stat_identity(source_info)
            destination_name = _collision_destination_name(
                destination_parent_fd,
                os.path.basename(original_path),
            )
            destination = os.path.join(parent, destination_name)

            # Both text paths must still identify the already pinned parents,
            # and the source name must still be the exact verified inode.
            if (
                not _directory_path_matches_fd(
                    source_parent,
                    source_parent_fd,
                )
                or not _directory_path_matches_fd(
                    parent,
                    destination_parent_fd,
                )
                or _stat_identity(
                    _entry_stat(source_parent_fd, source_name)
                )
                != expected_source_identity
            ):
                raise HistoryValidationError(
                    "restore path changed after verification")

            _renameat_noreplace(
                source_parent_fd,
                source_name,
                destination_parent_fd,
                destination_name,
            )
            if (
                not _directory_path_matches_fd(
                    source_parent,
                    source_parent_fd,
                )
                or not _directory_path_matches_fd(
                    parent,
                    destination_parent_fd,
                )
            ):
                try:
                    _renameat_noreplace(
                        destination_parent_fd,
                        destination_name,
                        source_parent_fd,
                        source_name,
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        "restore parent changed; rollback failed: "
                        f"{rollback_error}"
                    ) from rollback_error
                raise HistoryValidationError(
                    "restore parent changed during publication")

            now = time.time_ns()
            item.update({
                "status": "restored",
                "restored_path": destination,
                "restored_ns": now,
            })
            item.pop("error", None)
            operation["updated_ns"] = max(now, operation["created_ns"])
            if all(
                candidate["status"] in {"restored", "skipped"}
                for candidate in operation["items"]
            ):
                operation["status"] = "restored"
            else:
                operation["status"] = "partial"
            try:
                _write(path, store)
            except Exception as write_error:
                # Keep disk state and the durable audit record consistent.
                try:
                    _renameat_noreplace(
                        destination_parent_fd,
                        destination_name,
                        source_parent_fd,
                        source_name,
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        "history update failed after restore; rollback failed: "
                        f"{rollback_error}"
                    ) from write_error
                raise
            return {
                "operation_id": operation_id,
                "original_path": original_path,
                "restored_path": destination,
                "collision": destination != original_path,
            }
        finally:
            if destination_parent_fd >= 0:
                os.close(destination_parent_fd)
            if source_parent_fd >= 0:
                os.close(source_parent_fd)


def restore_operation(
    operation_id: str,
    *,
    allowed_roots: Sequence[str],
    base_dir: str | None = None,
) -> list[dict[str, Any]]:
    """Restore every not-yet-restored item; refuse before moving if a trash path is absent."""
    operation = get_operation(operation_id, base_dir=base_dir)
    if operation is None:
        raise KeyError(operation_id)
    pending = [item for item in operation["items"] if item["status"] != "restored"]
    if any("trashed_path" not in item for item in pending):
        raise HistoryValidationError("all restored items require explicit trashed_path values")
    for item in pending:
        _preflight_restore_record(
            item,
            allowed_roots=allowed_roots,
        )
    return [
        restore_item(
            operation_id,
            item["original_path"],
            allowed_roots=allowed_roots,
            base_dir=base_dir,
        )
        for item in pending
    ]
