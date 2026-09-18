"""Streaming CSV and self-contained HTML reports for DupScan results.

All access to a scan result is duck-typed, so this module can be used by the
Qt application and tests without importing ``core`` (and without creating a
circular dependency).
"""

from __future__ import annotations

import csv
import html
import io
import os
import stat
import time
import uuid
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, TextIO


MAX_TEXT = 32_768

_IMAGE = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jpeg", ".jpg", ".png",
    ".psd", ".raw", ".svg", ".tif", ".tiff", ".webp",
}
_VIDEO = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg",
    ".mpg", ".mts", ".webm", ".wmv",
}
_AUDIO = {
    ".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav",
    ".wma",
}
_DOCUMENT = {
    ".csv", ".doc", ".docx", ".epub", ".key", ".md", ".numbers", ".odf", ".ods",
    ".odt", ".pages", ".pdf", ".ppt", ".pptx", ".rtf", ".tex", ".tsv", ".txt",
    ".xls", ".xlsx",
}
_ARCHIVE = {
    ".7z", ".bz2", ".dmg", ".gz", ".iso", ".rar", ".tar", ".tbz",
    ".tgz", ".txz", ".xz", ".zip",
}
_APPLICATION = {
    ".app", ".bundle", ".framework", ".kext", ".pkg", ".plugin", ".xcodeproj",
}
_CATEGORY_LABELS = {
    "image": "Images",
    "video": "Video",
    "audio": "Audio",
    "document": "Documents",
    "archive": "Archives",
    "application": "Applications & packages",
    "other": "Other",
    "folder": "Folders",
}
_CSV_FIELDS = (
    "record_type",
    "group",
    "category",
    "path",
    "counterpart",
    "size_bytes",
    "copies",
    "selected",
    "reclaimable_bytes",
    "digest",
    "similarity_percent",
    "shared_bytes",
)


def categorize_path(path: str, *, directory: bool = False) -> str:
    """Return a stable, language-neutral category key."""
    if directory:
        return "folder"
    extension = os.path.splitext(path)[1].casefold()
    if extension in _IMAGE:
        return "image"
    if extension in _VIDEO:
        return "video"
    if extension in _AUDIO:
        return "audio"
    if extension in _DOCUMENT:
        return "document"
    if extension in _ARCHIVE:
        return "archive"
    if extension in _APPLICATION:
        return "application"
    return "other"


def _field(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or "\0" in value or len(value) > MAX_TEXT:
        raise ValueError(f"invalid report text in {label}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid Unicode in {label}") from exc
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0 or value > 2**63 - 1:
        raise ValueError(f"invalid report number in {label}")
    return value


def _paths(group: object, label: str) -> list[str]:
    values = _field(group, "paths", [])
    if not isinstance(values, (list, tuple)) or len(values) > 2_000_000:
        raise ValueError(f"invalid paths in {label}")
    return [_text(path, label) for path in values]


def _groups(result: object, name: str) -> Iterable[object]:
    value = _field(result, name, [])
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"invalid {name}")
    return value


def iter_report_rows(
    result: object, selected_paths: Iterable[str] = ()
) -> Iterator[dict[str, Any]]:
    """Yield normalized rows for exact files, duplicate folders and similar pairs."""
    selected = {_text(path, "selected_paths") for path in selected_paths}
    for index, group in enumerate(_groups(result, "file_groups"), 1):
        paths = _paths(group, "file group")
        size = _nonnegative_int(_field(group, "size"), "file size")
        digest = _text(_field(group, "digest", ""), "digest")
        selected_left = min(sum(path in selected for path in paths), max(0, len(paths) - 1))
        for path in paths:
            reclaimable = size if path in selected and selected_left > 0 else 0
            if reclaimable:
                selected_left -= 1
            yield {
                "record_type": "exact_file",
                "group": f"F{index}",
                "category": categorize_path(path),
                "path": path,
                "counterpart": "",
                "size_bytes": size,
                "copies": len(paths),
                "selected": path in selected,
                "reclaimable_bytes": reclaimable,
                "digest": digest,
                "similarity_percent": "",
                "shared_bytes": "",
            }
    for index, group in enumerate(_groups(result, "dir_groups"), 1):
        paths = _paths(group, "directory group")
        size = _nonnegative_int(_field(group, "size"), "directory size")
        selected_left = min(sum(path in selected for path in paths), max(0, len(paths) - 1))
        for path in paths:
            reclaimable = size if path in selected and selected_left > 0 else 0
            if reclaimable:
                selected_left -= 1
            yield {
                "record_type": "exact_directory",
                "group": f"D{index}",
                "category": "folder",
                "path": path,
                "counterpart": "",
                "size_bytes": size,
                "copies": len(paths),
                "selected": path in selected,
                "reclaimable_bytes": reclaimable,
                "digest": "",
                "similarity_percent": "",
                "shared_bytes": "",
            }
    for index, pair in enumerate(_groups(result, "sim_pairs"), 1):
        path_a = _text(_field(pair, "dir_a"), "similar directory")
        path_b = _text(_field(pair, "dir_b"), "similar directory")
        percent = _field(pair, "percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool):
            raise ValueError("invalid similarity percent")
        percent = float(percent)
        if not 0.0 <= percent <= 100.0:
            raise ValueError("similarity percent is outside 0..100")
        shared_bytes = _nonnegative_int(_field(pair, "shared_bytes"), "shared bytes")
        yield {
            "record_type": "similar_directories",
            "group": f"S{index}",
            "category": "folder",
            "path": path_a,
            "counterpart": path_b,
            "size_bytes": "",
            "copies": 2,
            "selected": False,
            "reclaimable_bytes": 0,
            "digest": "",
            "similarity_percent": f"{percent:.2f}",
            "shared_bytes": shared_bytes,
        }


def summarize_result(result: object, selected_paths: Iterable[str] = ()) -> dict[str, Any]:
    """Calculate totals without retaining all report rows in memory."""
    selected = tuple(selected_paths)
    summary: dict[str, Any] = {
        "exact_file_groups": 0,
        "exact_file_items": 0,
        "directory_groups": 0,
        "directory_items": 0,
        "similar_pairs": 0,
        "file_reclaimable_bytes": 0,
        "directory_reclaimable_bytes": 0,
        "selected_items": 0,
        "selected_bytes": 0,
        "selected_reclaimable_bytes": 0,
        "categories": {},
    }
    categories: dict[str, Counter[str]] = {}
    seen_groups: set[str] = set()
    for row in iter_report_rows(result, selected):
        record_type = row["record_type"]
        if record_type == "similar_directories":
            summary["similar_pairs"] += 1
            continue
        group_key = f"{record_type}:{row['group']}"
        if group_key not in seen_groups:
            seen_groups.add(group_key)
            potential = int(row["size_bytes"]) * max(0, int(row["copies"]) - 1)
            if record_type == "exact_file":
                summary["exact_file_groups"] += 1
                summary["file_reclaimable_bytes"] += potential
            else:
                summary["directory_groups"] += 1
                summary["directory_reclaimable_bytes"] += potential
        if record_type == "exact_file":
            summary["exact_file_items"] += 1
        else:
            summary["directory_items"] += 1
        category = row["category"]
        counter = categories.setdefault(category, Counter())
        counter["items"] += 1
        counter["bytes"] += int(row["size_bytes"])
        if row["selected"]:
            summary["selected_items"] += 1
            summary["selected_bytes"] += int(row["size_bytes"])
            counter["selected_items"] += 1
            counter["selected_bytes"] += int(row["size_bytes"])
        summary["selected_reclaimable_bytes"] += int(row["reclaimable_bytes"])
    summary["categories"] = {
        key: {
            "items": value["items"],
            "bytes": value["bytes"],
            "selected_items": value["selected_items"],
            "selected_bytes": value["selected_bytes"],
        }
        for key, value in sorted(categories.items())
    }
    return summary


def _safe_destination(path: str) -> tuple[str, str]:
    path = _text(path, "destination")
    if not os.path.isabs(path) or os.path.normpath(path) != path or not os.path.basename(path):
        raise ValueError("report destination must be an absolute normalized file path")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise FileNotFoundError(parent)
    if os.path.lexists(path):
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("report destination is not a safe regular file")
    return path, parent


@contextmanager
def _atomic_text(path: str, *, newline: str | None = None) -> Iterator[TextIO]:
    path, parent = _safe_destination(path)
    temp = os.path.join(parent, f".dupscan-report-{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp, flags, 0o600)
    stream = io.TextIOWrapper(
        io.FileIO(fd, mode="w", closefd=True), encoding="utf-8", newline=newline
    )
    try:
        yield stream
        stream.flush()
        os.fsync(stream.buffer.fileno())
        stream.close()
        os.replace(temp, path)
        try:
            parent_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError:
            # The file replacement is already atomic; directory fsync is an
            # extra durability measure unavailable on some filesystems.
            pass
    except Exception:
        try:
            stream.close()
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass
        raise


def _csv_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    value = _text(value, "CSV field")
    # CSV quoting does not stop spreadsheet formula injection.  A leading
    # apostrophe forces literal text in Numbers/Excel/LibreOffice.
    if value.lstrip(" \t\r").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(
    destination: str,
    result: object,
    *,
    selected_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Atomically stream a UTF-8 CSV report and return its summary."""
    selected = tuple(selected_paths)
    summary = summarize_result(result, selected)
    with _atomic_text(destination, newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in iter_report_rows(result, selected):
            writer.writerow({key: _csv_safe(row[key]) for key in _CSV_FIELDS})
    return summary


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if size < 1024 or unit == "PB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def _html_text(value: object) -> str:
    return html.escape(str(value), quote=True)


def export_html(
    destination: str,
    result: object,
    *,
    selected_paths: Iterable[str] = (),
    title: str = "DupScan Report",
    roots: Iterable[str] = (),
    generated_ns: int | None = None,
) -> dict[str, Any]:
    """Atomically stream a standalone, script-free and escaped HTML report."""
    title = _text(title, "title")
    root_values = [_text(root, "root") for root in roots]
    selected = tuple(selected_paths)
    summary = summarize_result(result, selected)
    timestamp = time.time_ns() if generated_ns is None else generated_ns
    if type(timestamp) is not int or timestamp <= 0:
        raise ValueError("invalid generated_ns")
    generated = datetime.fromtimestamp(timestamp / 1_000_000_000, timezone.utc).isoformat()
    errors_value = _field(result, "errors", [])
    if errors_value is None:
        errors_value = []
    if not isinstance(errors_value, (list, tuple)):
        raise ValueError("invalid scan errors")
    errors = [_text(error, "scan error") for error in errors_value]

    with _atomic_text(destination) as stream:
        stream.write("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
        stream.write("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
        stream.write(f"<title>{_html_text(title)}</title><style>")
        stream.write(
            "body{font:14px -apple-system,BlinkMacSystemFont,sans-serif;margin:32px;color:#172033;"
            "background:#f7f9fc}main{max-width:1400px;margin:auto}.cards{display:grid;"
            "grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}"
            ".card,table,.notice{background:white;border:1px solid #dbe2ee;border-radius:10px}"
            ".card{padding:16px}.number{font-size:22px;font-weight:700;margin-top:6px}"
            "table{width:100%;border-collapse:separate;border-spacing:0;overflow:hidden}"
            "th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #e7ebf2;"
            "vertical-align:top}th{background:#eef3fa;position:sticky;top:0}"
            "td.path{overflow-wrap:anywhere;max-width:520px}.yes{color:#08783e;font-weight:600}"
            ".notice{padding:14px;margin:14px 0}ul{margin-bottom:20px}small{color:#667085}"
        )
        stream.write("</style></head><body><main>")
        stream.write(
            f"<h1>{_html_text(title)}</h1>"
            f"<small>Generated {_html_text(generated)}</small>"
        )
        if root_values:
            stream.write("<h2>Scanned roots</h2><ul>")
            for root in root_values:
                stream.write(f"<li>{_html_text(root)}</li>")
            stream.write("</ul>")
        stream.write("<section class=\"cards\">")
        cards = (
            ("Exact file groups", summary["exact_file_groups"]),
            ("Duplicate folder groups", summary["directory_groups"]),
            ("Similar folder pairs", summary["similar_pairs"]),
            ("Selected items", summary["selected_items"]),
            ("Selected size", _format_bytes(summary["selected_bytes"])),
            ("Safely reclaimable selection", _format_bytes(summary["selected_reclaimable_bytes"])),
            ("Potential from files", _format_bytes(summary["file_reclaimable_bytes"])),
            ("Potential from folders", _format_bytes(summary["directory_reclaimable_bytes"])),
        )
        for label, value in cards:
            stream.write(
                f"<div class=\"card\"><div>{_html_text(label)}</div>"
                f"<div class=\"number\">{_html_text(value)}</div></div>"
            )
        stream.write("</section>")
        if summary["categories"]:
            stream.write("<h2>Categories</h2><table><thead><tr><th>Category</th><th>Items</th>")
            stream.write("<th>Size</th><th>Selected</th></tr></thead><tbody>")
            for category, values in summary["categories"].items():
                label = _CATEGORY_LABELS.get(category, category)
                stream.write(
                    f"<tr><td>{_html_text(label)}</td><td>{values['items']}</td>"
                    f"<td>{_html_text(_format_bytes(values['bytes']))}</td>"
                    f"<td>{values['selected_items']}</td></tr>"
                )
            stream.write("</tbody></table>")
        if errors:
            stream.write("<h2>Scan issues</h2><div class=\"notice\"><ul>")
            for error in errors:
                stream.write(f"<li>{_html_text(error)}</li>")
            stream.write("</ul></div>")
        stream.write("<h2>Results</h2><table><thead><tr>")
        headings = (
            "Type", "Group", "Category", "Path", "Compared with", "Size", "Copies",
            "Selected", "Reclaimable", "Digest / similarity",
        )
        for heading in headings:
            stream.write(f"<th>{_html_text(heading)}</th>")
        stream.write("</tr></thead><tbody>")
        for row in iter_report_rows(result, selected):
            descriptor = row["digest"] or (
                f"{row['similarity_percent']}%" if row["similarity_percent"] != "" else ""
            )
            values = (
                row["record_type"],
                row["group"],
                _CATEGORY_LABELS.get(row["category"], row["category"]),
                row["path"],
                row["counterpart"],
                _format_bytes(row["size_bytes"]) if row["size_bytes"] != "" else "",
                row["copies"],
                "Yes" if row["selected"] else "No",
                _format_bytes(row["reclaimable_bytes"]),
                descriptor,
            )
            stream.write("<tr>")
            for position, value in enumerate(values):
                class_name = " class=\"path\"" if position in (3, 4) else ""
                if position == 7 and value == "Yes":
                    class_name = " class=\"yes\""
                stream.write(f"<td{class_name}>{_html_text(value)}</td>")
            stream.write("</tr>")
        stream.write("</tbody></table></main></body></html>")
    return summary


def write_merge_plan_csv(
    path: str, plan: Iterable[tuple[int, str, str]],
    src_dir: str, dst_dir: str,
) -> int:
    """ПОВНИЙ план злиття у CSV — dry-run звіт перед дією.

    Прев'ю в діалозі обрізане до 15 рядків; для великих злиттів рішення
    «так/ні» вимагає переглянути ВСЕ. Пишеться
    атомарно (tmp+rename), викликач тримає запис поза GUI-потоком.
    symlink-прапорець знімається на момент запису — це знімок наміру, не
    доказ (доказ, як завжди, береться упритул до дії).

    Повертає кількість записаних рядків плану.
    """
    written = 0
    temporary = f"{path}.tmp-{uuid.uuid4().hex[:8]}"
    with open(temporary, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["розмір", "джерело", "ціль", "тип"])
        for size, source, rel in plan:
            writer.writerow([
                int(size), source, os.path.join(dst_dir, rel),
                "symlink" if os.path.islink(source) else "",
            ])
            written += 1
    os.replace(temporary, path)
    return written
