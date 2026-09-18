"""Product-facing helpers for categories, comparisons and problem summaries.

The scanner core deliberately stays focused on trustworthy content identity.
This module turns that identity graph into user-facing concepts without doing
disk I/O, so filtering and opening comparison views remain instantaneous.
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field


CATEGORY_LABELS = {
    "all": "Усі категорії",
    "folder": "Папки",
    "image": "Зображення",
    "video": "Відео",
    "audio": "Аудіо",
    "document": "Документи",
    "archive": "Архіви",
    "application": "Програми й пакети",
    "other": "Інше",
}

_EXTENSIONS = {
    "image": {
        ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jpeg",
        ".jpg", ".png", ".psd", ".raw", ".svg", ".tif", ".tiff", ".webp",
    },
    "video": {
        ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
        ".mpeg", ".mpg", ".mts", ".webm", ".wmv",
    },
    "audio": {
        ".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus",
        ".wav", ".wma",
    },
    "document": {
        ".csv", ".doc", ".docx", ".epub", ".key", ".md", ".numbers", ".ods",
        ".odt", ".pages", ".pdf", ".ppt", ".pptx", ".rtf", ".tex", ".txt",
        ".xls", ".xlsx",
    },
    "archive": {
        ".7z", ".bz2", ".dmg", ".gz", ".iso", ".rar", ".tar", ".tgz",
        ".xz", ".zip",
    },
    "application": {
        ".app", ".bundle", ".framework", ".kext", ".pkg", ".plugin",
        ".xcodeproj",
    },
}


def category_for_path(path: str) -> str:
    """Return a stable category id using only the filename extension."""
    lower = path.casefold().rstrip(os.sep)
    for category, extensions in _EXTENSIONS.items():
        if any(lower.endswith(ext) for ext in extensions):
            return category
    return "other"


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, CATEGORY_LABELS["other"])


def category_summary(paths: list[str] | set[str], file_meta: dict) -> dict[str, tuple[int, int]]:
    """Return ``category -> (item count, bytes)`` for a selection."""
    totals: dict[str, list[int]] = {}
    for path in paths:
        category = category_for_path(path)
        value = totals.setdefault(category, [0, 0])
        value[0] += 1
        meta = file_meta.get(path)
        if meta is not None:
            value[1] += max(0, int(meta.size))
    return {key: (value[0], value[1]) for key, value in totals.items()}


def _files_under(result, root: str) -> list[str]:
    root = os.path.abspath(root)
    out: list[str] = []
    stack = [root]
    visited: set[str] = set()
    while stack:
        directory = stack.pop()
        if directory in visited:
            continue
        visited.add(directory)
        out.extend(result.dir_files.get(directory, ()))
        stack.extend(result.dir_children.get(directory, ()))
    return out


@dataclass(frozen=True)
class FolderCompareRow:
    relative_path: str
    status: str
    path_a: str | None = None
    path_b: str | None = None
    size_a: int = 0
    size_b: int = 0

    @property
    def bytes(self) -> int:
        return max(self.size_a, self.size_b)


@dataclass
class FolderComparison:
    dir_a: str
    dir_b: str
    rows: list[FolderCompareRow] = field(default_factory=list)

    @property
    def counts(self) -> Counter:
        return Counter(row.status for row in self.rows)

    @property
    def shared_bytes(self) -> int:
        return sum(row.bytes for row in self.rows
                   if row.status in {"identical", "shared_elsewhere"})


def compare_folders(result, dir_a: str, dir_b: str) -> FolderComparison:
    """Compare two scanned directories by relative path *and* content class.

    Statuses are stable API values used by the UI and reports:
    ``identical``, ``shared_elsewhere``, ``different_content``, ``only_a`` and
    ``only_b``. No filesystem call is made here.
    """
    dir_a, dir_b = os.path.abspath(dir_a), os.path.abspath(dir_b)

    def index(root: str) -> dict[str, str]:
        indexed: dict[str, str] = {}
        for path in _files_under(result, root):
            try:
                rel = os.path.relpath(path, root)
            except ValueError:
                continue
            if rel == ".." or rel.startswith(".." + os.sep):
                continue
            indexed[rel] = path
        return indexed

    paths_a, paths_b = index(dir_a), index(dir_b)
    classes_a = {result.file_class.get(path) for path in paths_a.values()}
    classes_b = {result.file_class.get(path) for path in paths_b.values()}
    rows: list[FolderCompareRow] = []
    for rel in sorted(paths_a.keys() | paths_b.keys(), key=str.casefold):
        path_a, path_b = paths_a.get(rel), paths_b.get(rel)
        meta_a = result.file_meta.get(path_a) if path_a else None
        meta_b = result.file_meta.get(path_b) if path_b else None
        class_a = result.file_class.get(path_a) if path_a else None
        class_b = result.file_class.get(path_b) if path_b else None
        if path_a and path_b:
            status = "identical" if class_a == class_b else "different_content"
        elif path_a:
            status = "shared_elsewhere" if class_a in classes_b else "only_a"
        else:
            status = "shared_elsewhere" if class_b in classes_a else "only_b"
        rows.append(FolderCompareRow(
            relative_path=rel,
            status=status,
            path_a=path_a,
            path_b=path_b,
            size_a=int(meta_a.size) if meta_a else 0,
            size_b=int(meta_b.size) if meta_b else 0,
        ))
    return FolderComparison(dir_a, dir_b, rows)


def duplicate_explanation(result, path: str) -> str:
    """Explain the content evidence for a duplicate in plain Ukrainian."""
    class_id = result.file_class.get(path)
    candidates = result.class_paths.get(class_id, ())
    independent = {
        (result.file_meta[p].dev, result.file_meta[p].ino)
        for p in candidates if p in result.file_meta
    }
    if class_id and not class_id.startswith("u:") and len(independent) >= 2:
        return ("Повний BLAKE3 і розмір збігаються; знайдено "
                f"{len(independent)} незалежні файлові копії.")
    return "Незалежну повну копію не підтверджено; видалення буде заблоковано."


def classify_problem(message: str) -> tuple[str, str]:
    """Return a short problem category and actionable suggestion."""
    text = message.casefold()
    if "permission" in text or "denied" in text or "дозвол" in text:
        return "Немає доступу", "Перевірте Full Disk Access і права доступу до теки."
    if "stale" in text or "змінив" in text:
        return "Файл змінився", "Повторіть сканування після завершення запису файла."
    if "input/output" in text or "i/o" in text or "eio" in text:
        return "Помилка носія", "Перевірте диск у Disk Utility та повторіть сканування."
    if "fskit" in text:
        # "fskit" — стабільний, однозначний ключ: рядок з'являється ЛИШЕ у
        # повідомленні, яке core.scan_one_dir сам конструює для цього
        # випадку (не є підрядком жодного стандартного тексту OSError), тож
        # плутанини зі справжнім зникненням файла ("no such"/"not found"
        # нижче) немає — перевірка свідомо ПЕРЕД тим гілком.
        return (
            "Драйвер не відкриває файл",
            "macOS-драйвер exFAT (fskit) перелічує файл, але не відкриває. "
            "Файл цілий — скопіюйте його з диска через інший комп'ютер "
            "(Windows/Linux) або сторонній драйвер (Paragon), тоді DupScan "
            "його побачить.",
        )
    if "not found" in text or "no such" in text:
        return "Елемент зник", "Оновіть результати: файл переміщено або видалено."
    return "Помилка читання", "Відкрийте шлях у Finder, перевірте носій і повторіть сканування."
