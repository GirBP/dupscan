"""Local, privacy-aware diagnostics bundle export for DupScan.

The exporter accepts already-produced diagnostic values.  It never opens a
scanned file or reads a path supplied by the caller.  Raw scanned paths are
excluded unless ``include_scanned_paths=True`` is passed after explicit user
consent; paths embedded in logs, errors, and settings remain redacted.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import tempfile
import zipfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Final

from dupscan.version import BUILD, VERSION


DIAGNOSTICS_SCHEMA_VERSION: Final = 1
MAX_LOG_BYTES: Final = 512 * 1024
MAX_ERRORS_BYTES: Final = 256 * 1024
MAX_SETTINGS_BYTES: Final = 256 * 1024
MAX_SCANNED_PATHS_BYTES: Final = 256 * 1024
MAX_ARCHIVE_BYTES: Final = 2 * 1024 * 1024
MAX_COLLECTION_ITEMS: Final = 2_000
MAX_VALUE_DEPTH: Final = 10
MAX_TEXT_VALUE_BYTES: Final = 4 * 1024
MAX_SCANNED_PATH_VALUE_BYTES: Final = 16 * 1024
_TRUNCATION_MARKER = "\n… <truncated>\n"


class DiagnosticsError(Exception):
    """The diagnostics bundle could not be safely produced."""


@dataclass(frozen=True)
class DiagnosticsExportResult:
    destination: Path
    bytes_written: int
    entries: tuple[str, ...]
    truncated_entries: tuple[str, ...]
    included_scanned_paths: bool


@dataclass
class _SanitizeBudget:
    remaining_nodes: int = MAX_COLLECTION_ITEMS

    def consume(self) -> bool:
        if self.remaining_nodes <= 0:
            return False
        self.remaining_nodes -= 1
        return True


def export_diagnostics_bundle(
    destination: str | os.PathLike[str],
    *,
    app_version: str = VERSION,
    build: int = BUILD,
    logs: str | Iterable[str] = "",
    errors: object = (),
    # Mapping[str, Any] замість Mapping[object, object] —
    # ціль зрештою JSON (settings.json, рядок ~104), тож ключі й так мусять
    # бути рядками; єдиний живий викликач (app.py) уже дає dict[str, Any].
    # Рантайм-перевірка isinstance(settings, Mapping) нижче лишається як
    # захист від будь-якого некоректного входу незалежно від типізації.
    settings: Mapping[str, Any] | None = None,
    scanned_paths: Iterable[str | os.PathLike[str]] = (),
    include_scanned_paths: bool = False,
) -> DiagnosticsExportResult:
    """Atomically export a size-bounded ZIP containing sanitized diagnostics.

    ``include_scanned_paths`` must only be set in response to an explicit user
    choice.  Even then, no file is opened and no file content can enter the
    archive through this API.
    """

    target = _validate_destination(destination)
    if (
        not isinstance(app_version, str)
        or not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,127}", app_version)
    ):
        raise DiagnosticsError("Invalid application version")
    if type(build) is not int or build < 0:
        raise DiagnosticsError("Invalid application build")
    if not isinstance(include_scanned_paths, bool):
        raise DiagnosticsError("Path inclusion choice must be a boolean")
    if settings is not None and not isinstance(settings, Mapping):
        raise DiagnosticsError("Settings diagnostics must be a mapping")

    sanitized_logs, logs_truncated = _prepare_logs(logs)
    sanitized_errors = _sanitize_errors(errors)
    sanitized_settings = _sanitize_value(
        settings or {},
        budget=_SanitizeBudget(),
        ancestors=set(),
        depth=0,
    )
    errors_payload, errors_truncated = _json_payload(sanitized_errors, MAX_ERRORS_BYTES)
    settings_payload, settings_truncated = _json_payload(
        sanitized_settings, MAX_SETTINGS_BYTES
    )
    paths_payload, path_count, paths_truncated = _prepare_scanned_paths(
        scanned_paths, include=include_scanned_paths
    )

    truncated: list[str] = []
    if logs_truncated:
        truncated.append("logs.txt")
    if errors_truncated:
        truncated.append("errors.json")
    if settings_truncated:
        truncated.append("settings.json")
    if paths_truncated:
        truncated.append("scanned_paths.json")

    metadata = {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "application": {
            "name": "DupScan",
            "version": app_version,
            "build": build,
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "privacy": {
            "paths_in_logs_errors_settings_redacted": True,
            "scanned_paths_included": include_scanned_paths,
            "file_contents_included": False,
        },
        "scanned_path_count": path_count,
        "truncated_entries": truncated,
    }
    metadata_payload = _encode_json(metadata)

    entries: dict[str, bytes] = {
        "metadata.json": metadata_payload,
        "logs.txt": sanitized_logs,
        "errors.json": errors_payload,
        "settings.json": settings_payload,
    }
    if paths_payload is not None:
        entries["scanned_paths.json"] = paths_payload

    if sum(len(payload) for payload in entries.values()) > MAX_ARCHIVE_BYTES - 4096:
        raise DiagnosticsError("Diagnostics payload exceeded its total size limit")
    archive_size = _write_zip_atomically(target, entries)
    return DiagnosticsExportResult(
        destination=target,
        bytes_written=archive_size,
        entries=tuple(entries),
        truncated_entries=tuple(truncated),
        included_scanned_paths=include_scanned_paths,
    )


def redact_sensitive_text(value: str) -> str:
    """Redact common absolute paths and credential-shaped values from text."""

    if not isinstance(value, str):
        raise DiagnosticsError("Diagnostic text must be a string")
    redacted = _FILE_URI_RE.sub("<PATH>", value)
    redacted = _WINDOWS_PATH_RE.sub("<PATH>", redacted)
    redacted = _HOME_PATH_RE.sub("<PATH>", redacted)
    redacted = _KNOWN_POSIX_PATH_RE.sub("<PATH>", redacted)
    redacted = _GENERIC_POSIX_PATH_RE.sub("<PATH>", redacted)
    redacted = _BEARER_RE.sub("Bearer <REDACTED>", redacted)
    redacted = _CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}=<REDACTED>", redacted)
    redacted = _EMAIL_RE.sub("<EMAIL>", redacted)
    return redacted


def _validate_destination(value: str | os.PathLike[str]) -> Path:
    try:
        target = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise DiagnosticsError("Invalid diagnostics destination") from exc
    if not target.name or target.name in {".", ".."}:
        raise DiagnosticsError("Invalid diagnostics destination")
    parent = target.parent
    if not parent.is_dir():
        raise DiagnosticsError("Diagnostics destination directory does not exist")
    if target.exists() and target.is_dir():
        raise DiagnosticsError("Diagnostics destination is a directory")
    return target.absolute()


def _prepare_logs(logs: str | Iterable[str]) -> tuple[bytes, bool]:
    if isinstance(logs, str):
        chunks: Iterable[object] = (logs,)
    elif isinstance(logs, (bytes, bytearray)):
        raise DiagnosticsError("Logs must be text, not bytes")
    else:
        try:
            chunks = iter(logs)
        except TypeError as exc:
            raise DiagnosticsError("Logs must be text or an iterable of text") from exc

    output = bytearray()
    truncated = False
    for index, chunk in enumerate(chunks):
        if index >= MAX_COLLECTION_ITEMS:
            truncated = True
            break
        if not isinstance(chunk, str):
            chunk = f"<unsupported:{type(chunk).__name__}>"
        if len(chunk) > MAX_LOG_BYTES:
            chunk = "<oversized-log-entry-truncated>"
            truncated = True
        sanitized = redact_sensitive_text(chunk)
        if index:
            sanitized = "\n" + sanitized
        encoded = sanitized.encode("utf-8", errors="replace")
        if len(output) + len(encoded) > MAX_LOG_BYTES:
            remaining = max(0, MAX_LOG_BYTES - len(output))
            output.extend(_truncate_utf8(encoded, remaining, marker=_TRUNCATION_MARKER))
            truncated = True
            break
        output.extend(encoded)
    return bytes(output), truncated


def _sanitize_errors(errors: object) -> object:
    if isinstance(errors, str) or isinstance(errors, Mapping):
        source: object = errors
    elif isinstance(errors, (bytes, bytearray)):
        source = "<unsupported:bytes>"
    else:
        try:
            # Той самий випадок, що й scale_ui.summarize_
            # candidates — реальний код call-overload, попередній
            # arg-type-коментар нічого не гасив.
            iterator = iter(errors)  # type: ignore[call-overload]
        except TypeError:
            source = errors
        else:
            collected: list[object] = []
            for index, item in enumerate(iterator):
                if index >= MAX_COLLECTION_ITEMS:
                    collected.append("<collection-truncated>")
                    break
                collected.append(item)
            source = collected
    return _sanitize_value(source, budget=_SanitizeBudget(), ancestors=set(), depth=0)


def _sanitize_value(
    value: object,
    *,
    budget: _SanitizeBudget,
    ancestors: set[int],
    depth: int,
) -> object:
    if not budget.consume():
        return "<collection-truncated>"
    if depth > MAX_VALUE_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite-number>"
    if isinstance(value, os.PathLike):
        return "<PATH>"
    if isinstance(value, str):
        if _is_path_value(value):
            return "<PATH>"
        return _bounded_sanitized_string(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Bytes can be file content or secrets; never serialize them.
        return f"<binary-data:{len(value)}-bytes-omitted>"

    identity = id(value)
    if identity in ancestors:
        return "<cycle>"
    next_ancestors = {*ancestors, identity}
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                result["<collection-truncated>"] = True
                break
            if isinstance(key, os.PathLike):
                key_text = "<PATH>"
            elif type(key) in {str, int, float, bool} or key is None:
                key_text = _bounded_sanitized_string(str(key), max_bytes=512)
            else:
                key_text = f"<unsupported-key:{type(key).__name__}>"
            if _is_sensitive_key(key_text):
                result[key_text] = "<REDACTED>"
            else:
                result[key_text] = _sanitize_value(
                    child,
                    budget=budget,
                    ancestors=next_ancestors,
                    depth=depth + 1,
                )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        result_list: list[object] = []
        for index, child in enumerate(value):
            if index >= MAX_COLLECTION_ITEMS:
                result_list.append("<collection-truncated>")
                break
            result_list.append(
                _sanitize_value(
                    child,
                    budget=budget,
                    ancestors=next_ancestors,
                    depth=depth + 1,
                )
            )
        return result_list
    # Avoid calling repr()/str() on arbitrary objects: either could expose file
    # contents, paths, credentials, or trigger user-controlled code.
    return f"<unsupported:{type(value).__name__}>"


def _prepare_scanned_paths(
    paths: Iterable[str | os.PathLike[str]], *, include: bool
) -> tuple[bytes | None, int | None, bool]:
    if not include:
        try:
            count = len(paths)  # type: ignore[arg-type]
        except (TypeError, OverflowError):
            count = None
        return None, count, False

    if isinstance(paths, (str, bytes, bytearray)):
        raise DiagnosticsError("Scanned paths must be an iterable of path values")
    collected: list[str] = []
    truncated = False
    try:
        iterator = iter(paths)
    except TypeError as exc:
        raise DiagnosticsError("Scanned paths must be iterable") from exc
    for index, path in enumerate(iterator):
        if index >= MAX_COLLECTION_ITEMS:
            truncated = True
            break
        try:
            path_text = os.fspath(path)
        except TypeError as exc:
            raise DiagnosticsError("A scanned path was not a path value") from exc
        if not isinstance(path_text, str):
            raise DiagnosticsError("Byte paths are not accepted in diagnostics")
        if "\x00" in path_text:
            raise DiagnosticsError("A scanned path contained a NUL character")
        if len(path_text.encode("utf-8")) > MAX_SCANNED_PATH_VALUE_BYTES:
            raise DiagnosticsError("A scanned path exceeded its size limit")
        collected.append(path_text)
    payload, size_truncated = _json_payload(collected, MAX_SCANNED_PATHS_BYTES)
    return payload, len(collected), truncated or size_truncated


def _json_payload(value: object, max_bytes: int) -> tuple[bytes, bool]:
    encoded = _encode_json(value)
    if len(encoded) <= max_bytes:
        return encoded, False
    fallback = _encode_json(
        {
            "truncated": True,
            "reason": "entry size limit",
            "original_bytes": len(encoded),
        }
    )
    return fallback, True


def _encode_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _bounded_sanitized_string(value: str, *, max_bytes: int = MAX_TEXT_VALUE_BYTES) -> str:
    if len(value) > max_bytes:
        return "<oversized-text-omitted>"
    redacted = redact_sensitive_text(value)
    encoded = redacted.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return redacted
    return "<oversized-text-omitted>"


def _truncate_utf8(data: bytes, limit: int, *, marker: str) -> bytes:
    if len(data) <= limit:
        return data
    if limit <= 0:
        return b""
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= limit:
        return marker_bytes[:limit].decode("utf-8", errors="ignore").encode("utf-8")
    prefix = data[: limit - len(marker_bytes)].decode("utf-8", errors="ignore").encode("utf-8")
    return prefix + marker_bytes


def _is_path_value(value: str) -> bool:
    if not value:
        return False
    expanded = os.path.expanduser(value)
    return (
        value.startswith(("~/", "file://", "\\\\"))
        or os.path.isabs(expanded)
        or PureWindowsPath(value).is_absolute()
    )


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(
        token in normalized
        for token in (
            "password",
            "passwd",
            "secret",
            "token",
            "apikey",
            "authorization",
            "cookie",
            "privatekey",
        )
    )


def _write_zip_atomically(target: Path, entries: Mapping[str, bytes]) -> int:
    descriptor = -1
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        descriptor = -1
        timestamp = datetime.now(timezone.utc)
        zip_timestamp = (
            max(1980, timestamp.year),
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second,
        )
        with zipfile.ZipFile(
            temporary_name,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=False,
        ) as archive:
            for name, payload in entries.items():
                info = zipfile.ZipInfo(name, date_time=zip_timestamp)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                archive.writestr(info, payload)

        archive_size = os.path.getsize(temporary_name)
        if archive_size > MAX_ARCHIVE_BYTES:
            raise DiagnosticsError("Diagnostics archive exceeded its size limit")
        with open(temporary_name, "rb") as archive_file:
            os.fsync(archive_file.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
        return archive_size
    except DiagnosticsError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise DiagnosticsError("Could not write the diagnostics archive") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


_FILE_URI_RE = re.compile(r"(?i)\bfile://(?:localhost)?/[^\r\n\"'<>]+")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:[A-Z]:[\\/]|\\\\)[^\r\n\"'<>|]+"
)
_HOME_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])~/[^\r\n\"'<>:,;\)\]\}]+")
_KNOWN_POSIX_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9_])/(?:Users|home|Volumes|private|tmp)"
    r"(?:/[^\r\n\"'<>:,;\)\]\}]+)+"
)
_GENERIC_POSIX_PATH_RE = re.compile(
    r"(?<![:/A-Za-z0-9_])/(?:[^/\s\"'<>:,;\(\)\[\]\{\}]+/)*"
    r"[^/\s\"'<>:,;\(\)\[\]\{\}]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_CREDENTIAL_RE = re.compile(
    r"(?i)\b(password|passwd|token|api[_-]?key|authorization|cookie)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_EMAIL_RE = re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
