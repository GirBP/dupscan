"""Сесії сканів DupScan: стиснені JSON-знімки внутрішнього стану ScanResult у
<base_dir>/sessions/<time_ns()>.json.gz. Старі ``.json`` читаються без
міграції. groups (file_groups/dir_groups/
sim_pairs) НЕ серіалізуються — вони відновлюються з нуля через
core._aggregate() при завантаженні. best-effort: помилка запису чи
пошкоджений json ніколи не валить виклик.
"""

from __future__ import annotations

import gzip
import errno
import fcntl
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager

import ijson

import dupscan.domain.core as core

_KEEP = 30
_MAX_SESSION_BYTES = 512 * 1024 * 1024
_MAX_SESSION_STORE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_STATE_ITEMS = 2_000_000

_META_FIELDS = ("version", "created_ns", "roots", "partial",
                "files_seen", "bytes_seen", "errors_total", "counts")

_SUPPORTED_SESSION_VERSIONS = (1, 2, 3)


def _unsupported_version_message(version: object) -> str:
    """Текст відмови за версією сесії.

    Число БІЛЬШЕ за все підтримуване — це, найімовірніше, сесія з
    майбутньої версії застосунку (новий формат, невідомі поля), а не
    пошкоджений файл: власник має зрозуміти, що треба ОНОВИТИ застосунок,
    а не запідозрити биту сесію. Інші випадки версії (відсутня, не int,
    0 чи від'ємна) лишаються загальним формулюванням — там немає підстави
    стверджувати щось конкретне про причину.
    """
    if (isinstance(version, int) and not isinstance(version, bool)
            and version > max(_SUPPORTED_SESSION_VERSIONS)):
        return ("сесія новішої версії DupScan — оновіть застосунок, "
                "щоб її відкрити")
    return "непідтримувана версія сесії DupScan"


def _meta_path(payload_path: str) -> str:
    for suffix in (".json.gz", ".json"):
        if payload_path.endswith(suffix):
            return payload_path[: -len(suffix)] + ".meta.json"
    return payload_path + ".meta.json"


def _is_payload(name: str) -> bool:
    return (
        name.endswith(".json.gz")
        or (name.endswith(".json") and not name.endswith(".meta.json"))
    )


def _default_base_dir() -> str:
    return os.environ.get("DUPSCAN_DATA_DIR") or os.path.expanduser(
        "~/Library/Application Support/DupScan"
    )


def _sessions_dir(base_dir: str | None) -> str:
    base = base_dir if base_dir is not None else _default_base_dir()
    return os.path.join(base, "sessions")


@contextmanager
def _locked(base_dir: str | None):
    """Ексклюзивний fcntl-лок навколо запису й prune сховища сесій — два
    інстанси DupScan інакше можуть перетнутися:
    prune одного бачить ЧАСТКОВИЙ стан сусіда (застарілий os.listdir),
    може знести щойно написану пару або лишити торн-пару. За зразком
    removal_history._locked. Блокування коротке; best-effort деградація
    тут ЗАБОРОНЕНА (на відміну від кешу хешів, де відсутність — лише
    повільніше сканування): не взяв лок -> чекає — це фонові операції,
    а не щось на UI-потоці.
    """
    sdir = _sessions_dir(base_dir)
    os.makedirs(sdir, exist_ok=True)
    lock_path = os.path.join(sdir, ".lock")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield sdir
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _prune_old(
        sdir: str, keep: int = _KEEP,
        preserve_paths: tuple[str, ...] | list[str] = (),
        max_bytes: int = _MAX_SESSION_STORE_BYTES) -> None:
    try:
        names = sorted(n for n in os.listdir(sdir) if _is_payload(n))
    except OSError:
        return
    protected = {
        os.path.normcase(os.path.normpath(os.path.abspath(path)))
        for path in preserve_paths
    }
    def remove_pair(p: str) -> None:
        for victim in (p, _meta_path(p)):  # payload і сайдкар — парою
            try:
                os.remove(victim)
            except OSError:
                pass

    for n in names[:-keep] if keep > 0 else names:
        p = os.path.join(sdir, n)
        if os.path.normcase(os.path.normpath(os.path.abspath(p))) not in protected:
            remove_pair(p)

    # Count-only retention allowed a handful of very large sessions to fill
    # the system disk. Preserve explicitly protected snapshots even if that
    # temporarily exceeds the budget; all other oldest pairs are removable.
    try:
        remaining = [
            os.path.join(sdir, n)
            for n in sorted(os.listdir(sdir))
            if _is_payload(n)
        ]
        total = sum(
            os.path.getsize(path)
            + (os.path.getsize(_meta_path(path))
               if os.path.exists(_meta_path(path)) else 0)
            for path in remaining
        )
    except OSError:
        return
    for path in remaining:
        if total <= max_bytes:
            break
        if os.path.normcase(os.path.normpath(os.path.abspath(path))) in protected:
            continue
        try:
            pair_size = os.path.getsize(path)
            if os.path.exists(_meta_path(path)):
                pair_size += os.path.getsize(_meta_path(path))
        except OSError:
            pair_size = 0
        remove_pair(path)
        total = max(0, total - pair_size)


def _fsync_parent(path: str) -> None:
    """Best-effort directory durability after an atomic publication."""
    descriptor = -1
    try:
        descriptor = os.open(
            os.path.dirname(path),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        os.fsync(descriptor)
    except OSError:
        # Some network/removable filesystems do not support directory fsync.
        pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_write_document(path: str, writer, *, compressed: bool) -> None:
    """Write one complete JSON document then atomically publish it."""
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        if compressed:
            with gzip.open(
                tmp, "wt", encoding="utf-8", compresslevel=6
            ) as fh:
                writer(fh)
        else:
            with open(tmp, "w", encoding="utf-8") as fh:
                writer(fh)
                fh.flush()
                os.fsync(fh.fileno())
        # gzip.close() finalizes the stream but does not promise durable media.
        if compressed:
            with open(tmp, "rb") as durable:
                os.fsync(durable.fileno())
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _atomic_write_json(path: str, data: object, *, compressed: bool) -> None:
    _atomic_write_document(
        path,
        lambda stream: json.dump(
            data,
            stream,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        compressed=compressed,
    )


def _atomic_copy(source_path: str, destination_path: str) -> None:
    """Copy to a private sibling and publish without exposing partial bytes."""
    tmp = (
        f"{destination_path}.{os.getpid()}."
        f"{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with open(source_path, "rb") as source, open(tmp, "xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, destination_path)
        _fsync_parent(destination_path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _atomic_write_session(
    path: str,
    result: core.ScanResult,
    meta: dict,
) -> None:
    """Stream state directly to gzip without building a duplicate payload."""

    def write_document(stream) -> None:
        encoder = json.JSONEncoder(
            ensure_ascii=False,
            separators=(",", ":"),
        )
        pending: list[str] = []
        pending_chars = 0

        def emit(value: str) -> None:
            nonlocal pending_chars
            pending.append(value)
            pending_chars += len(value)
            if pending_chars >= 1024 * 1024:
                stream.write("".join(pending))
                pending.clear()
                pending_chars = 0

        def write_value(value: object) -> None:
            if isinstance(value, (list, tuple)) and len(value) > 256:
                emit("[")
                for index, item in enumerate(value):
                    if index:
                        emit(",")
                    write_value(item)
                emit("]")
                return
            emit(encoder.encode(value))

        first_top = True

        def top_field(name: str, value: object) -> None:
            nonlocal first_top
            if not first_top:
                emit(",")
            first_top = False
            write_value(name)
            emit(":")
            write_value(value)

        emit("{")
        for key in _META_FIELDS:
            top_field(key, meta[key])
        top_field("errors", result.errors)
        emit(',"state":{')
        first_state = True

        def state_prefix(name: str) -> None:
            nonlocal first_state
            if not first_state:
                emit(",")
            first_state = False
            write_value(name)
            emit(":")

        def mapping(name: str, items, transform=lambda value: value) -> None:
            state_prefix(name)
            emit("{")
            first = True
            for key, value in items:
                if not first:
                    emit(",")
                first = False
                write_value(key)
                emit(":")
                write_value(transform(value))
            emit("}")

        mapping(
            "file_meta",
            result.file_meta.items(),
            lambda info: [
                info.size,
                info.mtime_ns,
                info.btime_ns,
                info.ctime_ns,
                info.dev,
                info.ino,
            ],
        )
        mapping("file_class", result.file_class.items())
        # v3: class_size, class_paths і dir_files — похідні від file_class
        # (розмір закодовано у префіксі класу, і валідатор вимагає цієї
        # рівності) та dir_ok+file_meta; у файл не пишуться, відновлюються
        # при завантаженні. Це прибирає другу/третю копію кожного digest-а.
        mapping("dir_children", result.dir_children.items())
        mapping("dir_links", result.dir_links.items())
        mapping("dir_aliases", result.dir_aliases.items())
        mapping("dir_ok", result.dir_ok.items())
        mapping("file_storage", result.file_storage.items(),
                lambda sid: [sid[0], sid[1]])
        mapping("file_alloc", result.file_alloc.items())
        state_prefix("dir_read_failed")
        emit("[")
        for index, d in enumerate(sorted(result.dir_read_failed)):
            if index:
                emit(",")
            write_value(d)
        emit("]")
        state_prefix("ignored_pairs")
        emit("[")
        for index, pair in enumerate(sorted(result.ignored_pairs)):
            if index:
                emit(",")
            write_value(pair)
        emit("]}}")
        if pending:
            stream.write("".join(pending))

    _atomic_write_document(path, write_document, compressed=True)


def save_session(
    res: core.ScanResult,
    roots: list[str],
    partial: bool = False,
    base_dir: str | None = None,
    preserve_paths: tuple[str, ...] | list[str] = (),
) -> str:
    """Пише знімок res у sessions/<time_ns>.json.gz. Повертає шлях, або "" якщо
    запис не вдався (best-effort — ніколи не кидає, помилку лишає в res.errors)."""
    path = ""
    meta_path = ""
    try:
        # Запис + prune — критична секція, серіалізована fcntl-
        # локом проти другого інстансу DupScan у ТОМУ Ж sessions-dir.
        with _locked(base_dir) as sdir:
            ns = time.time_ns()
            path = os.path.join(sdir, f"{ns}.json.gz")
            meta_path = _meta_path(path)
            meta = {
                "version": 3,
                "created_ns": ns,
                "roots": list(roots),
                "partial": bool(partial),
                "files_seen": res.files_seen,
                "bytes_seen": res.bytes_seen,
                "errors_total": max(res.errors_total, len(res.errors)),
                "counts": {
                    "files": len(res.file_groups),
                    "dirs": len(res.dir_groups),
                    "pairs": len(res.sim_pairs),
                },
            }
            # Publish the lightweight sidecar first. A crash at this point
            # leaves an ignored sidecar, not a payload that list_sessions may
            # mistake for a successfully saved session. The payload is the
            # final publication.
            _write_meta(path, meta)
            _atomic_write_session(path, res, meta)
            _prune_old(sdir, preserve_paths=preserve_paths)
        return path
    except Exception as e:  # noqa: BLE001 — best-effort: сесія не має валити скан
        for victim in (path, meta_path):
            if victim:
                try:
                    os.remove(victim)
                except OSError:
                    pass
        try:
            core._record_error(res, f"не вдалось зберегти сесію: {e}")
        except Exception:  # noqa: BLE001
            pass
        return ""


def _write_meta(payload_path: str, meta: dict) -> None:
    _atomic_write_json(_meta_path(payload_path), meta, compressed=False)


def _pair_size(payload_path: str) -> int:
    size = 0
    for member in (payload_path, _meta_path(payload_path)):
        try:
            size += os.path.getsize(member)
        except OSError:
            pass
    return size


def compact_store(
    base_dir: str | None = None,
    *,
    legacy_age_days: int | None = None,
    max_bytes: int | None = None,
    preserve_paths: tuple[str, ...] | list[str] = (),
    dry_run: bool = False,
) -> dict:
    """Явне прибирання сховища сесій. НІКОЛИ не викликається автоматично —
    лише свідомою дією користувача (кнопка в UI).

    - legacy_age_days: видалити незжаті legacy-``.json`` старші за N днів
      (вік — за time_ns-іменем файлу);
    - max_bytes: retention за БАЙТАМИ — найстаріші пари payload+meta
      видаляються, поки сховище не влізе в бюджет;
    - найновіша сесія і preserve_paths недоторканні завжди;
    - dry_run: лише порахувати. Повертає
      {"removed", "freed_bytes", "store_bytes"} (store_bytes — після
      прибирання; для dry_run — прогноз).
    """
    # Та сама критична секція, що й save_session — читання списку й
    # видалення серіалізовані проти другого інстансу DupScan.
    with _locked(base_dir) as sdir:
        try:
            names = sorted(n for n in os.listdir(sdir) if _is_payload(n))
        except OSError:
            return {"removed": 0, "freed_bytes": 0, "store_bytes": 0}
        paths = [os.path.join(sdir, n) for n in names]  # старіші першими
        protected = {
            os.path.normcase(os.path.normpath(os.path.abspath(p)))
            for p in preserve_paths
        }
        if paths:  # найновіша сесія — остання робота користувача, не чіпаємо
            protected.add(os.path.normcase(
                os.path.normpath(os.path.abspath(paths[-1]))))

        def is_protected(path: str) -> bool:
            return os.path.normcase(
                os.path.normpath(os.path.abspath(path))) in protected

        sizes = {p: _pair_size(p) for p in paths}
        doomed: list[str] = []
        doomed_keys: set[str] = set()

        def doom(path: str) -> None:
            if is_protected(path):
                return
            key = os.path.normcase(path)
            if key not in doomed_keys:
                doomed_keys.add(key)
                doomed.append(path)

        if legacy_age_days is not None:
            cutoff_ns = time.time_ns() - legacy_age_days * 24 * 3600 * 10 ** 9
            for p in paths:
                name = os.path.basename(p)
                if name.endswith(".json.gz") or not name.endswith(".json"):
                    continue
                stem = name[: -len(".json")]
                if stem.isdigit() and int(stem) < cutoff_ns:
                    doom(p)
        if max_bytes is not None:
            total = sum(sizes.values()) - sum(
                sizes[p] for p in doomed)
            for p in paths:
                if total <= max_bytes:
                    break
                if os.path.normcase(p) in doomed_keys or is_protected(p):
                    continue
                doom(p)
                total -= sizes[p]
        freed = sum(sizes[p] for p in doomed)
        if not dry_run:
            for p in doomed:
                for victim in (p, _meta_path(p)):
                    try:
                        os.remove(victim)
                    except OSError:
                        pass
        store_bytes = sum(sizes.values()) - freed
        return {
            "removed": len(doomed),
            "freed_bytes": freed,
            "store_bytes": max(0, store_bytes),
        }


def list_sessions(base_dir: str | None = None) -> list[dict]:
    """Мета кожної сесії, найновіші перші. Читає ЛИШЕ *.meta.json-сайдкари
    (важкий payload НЕ парситься — список миттєвий незалежно від розміру
    сканів); стара сесія без сайдкара мігрується один раз. Пошкоджені файли
    пропускаються."""
    sdir = _sessions_dir(base_dir)
    try:
        names = [n for n in os.listdir(sdir) if _is_payload(n)]
    except OSError:
        return []
    out: list[dict] = []
    for n in names:
        p = os.path.join(sdir, n)
        try:
            with open(_meta_path(p), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # legacy-сесія без сайдкара: одноразова міграція з payload
            try:
                data = _read_validated(p)
                _write_meta(p, {k: data[k] for k in _META_FIELDS if k in data})
            except (OSError, ValueError, TypeError):
                continue
        try:
            out.append(
                {
                    "path": p,
                    "created_ns": int(data["created_ns"]),
                    "roots": list(data.get("roots", [])),
                    "partial": bool(data.get("partial", False)),
                    "counts": dict(
                        data.get("counts", {"files": 0, "dirs": 0, "pairs": 0})
                    ),
                    "files_seen": data.get("files_seen", 0),
                    "bytes_seen": data.get("bytes_seen", 0),
                }
            )
        except (ValueError, KeyError, TypeError):
            continue
    out.sort(key=lambda m: m["created_ns"], reverse=True)
    return out


def _safe_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"некоректний шлях у {label}")
    if len(value) > 32768 or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ValueError(f"небезпечний шлях у {label}: {value!r}")
    return value


# macOS-псевдоніми верхнього рівня: симлінки /var, /tmp, /etc → /private/*.
# Лексичне зведення БЕЗ звертання до диска: read-only парсинг історичної
# сесії не сміє резолвити корені (том може бути від'єднаний або мережевий).
_PRIVATE_ALIASES = ("/var", "/tmp", "/etc")


def _lexical_alias(root: str) -> str | None:
    for alias in _PRIVATE_ALIASES:
        if root == alias or root.startswith(alias + os.sep):
            return "/private" + root
    return None


def _allowed_root_prefixes(roots: list[str]) -> tuple[tuple[str, str], ...]:
    # Сумісність зі старими сесіями: воркери до 2.17 писали сирі корені
    # користувача (напр. /var/… замість /private/var/…), тоді як file_meta
    # завжди містить realpath-шляхи. Псевдонім того самого каталогу не
    # розширює containment.
    expanded: list[str] = []
    for root in roots:
        expanded.append(root)
        alias = _lexical_alias(root)
        if alias is not None:
            expanded.append(alias)
    return tuple(dict.fromkeys(
        (
            root,
            root if root.endswith(os.sep) else root + os.sep,
        )
        for root in expanded
    ))


def _in_allowed_roots(
    path: str,
    prefixes: tuple[tuple[str, str], ...],
) -> bool:
    # All inputs passed _safe_path(), so lexical component-boundary matching
    # is equivalent to commonpath here and avoids repeatedly splitting hundreds
    # of thousands of long paths during session validation.
    return any(path == root or path.startswith(prefix) for root, prefix in prefixes)


def _dict(value: object, label: str) -> dict:
    if not isinstance(value, dict) or len(value) > _MAX_STATE_ITEMS:
        raise ValueError(f"некоректний або завеликий розділ {label}")
    return value


def _validate_payload(data: object) -> dict:
    """Строга структурна й перехресна валідація недовіреної JSON-сесії."""
    if (not isinstance(data, dict)
            or data.get("version") not in _SUPPORTED_SESSION_VERSIONS):
        raise ValueError(_unsupported_version_message(
            data.get("version") if isinstance(data, dict) else None))
    if not isinstance(data.get("created_ns"), int):
        raise ValueError("у сесії немає коректного created_ns")
    roots = data.get("roots", [])
    if not isinstance(roots, list) or len(roots) > 10000:
        raise ValueError("некоректний список коренів")
    for root in roots:
        _safe_path(root, "roots")
    errors = data.get("errors", [])
    errors_total = data.get("errors_total", len(errors) if isinstance(errors, list) else 0)
    if (
        not isinstance(errors, list)
        or len(errors) > core.MAX_ERROR_DETAILS
        or not isinstance(errors_total, int)
        or errors_total < len(errors)
        or errors_total > _MAX_STATE_ITEMS
        or any(not isinstance(message, str) or len(message) > 32768
               for message in errors)
    ):
        raise ValueError("некоректний список помилок сесії")
    allowed_roots = _allowed_root_prefixes(roots)

    def in_roots(path: str) -> bool:
        return _in_allowed_roots(path, allowed_roots)
    state = _dict(data.get("state"), "state")
    meta = _dict(state.get("file_meta", {}), "file_meta")
    file_class = _dict(state.get("file_class", {}), "file_class")
    class_size = _dict(state.get("class_size", {}), "class_size")
    # v3 не зберігає похідні class_paths/dir_files (відновлюються при
    # завантаженні); у v1/v2 їх відсутність лишається помилкою.
    derived_optional = data.get("version") == 3
    class_paths_raw = state.get("class_paths")
    dir_files_raw = state.get("dir_files")
    class_paths = (
        None if derived_optional and class_paths_raw is None
        else _dict(class_paths_raw if class_paths_raw is not None else {},
                   "class_paths"))
    dir_files = (
        None if derived_optional and dir_files_raw is None
        else _dict(dir_files_raw if dir_files_raw is not None else {},
                   "dir_files"))
    dir_children = _dict(state.get("dir_children", {}), "dir_children")
    dir_links = _dict(state.get("dir_links", {}), "dir_links")
    dir_aliases = _dict(state.get("dir_aliases", {}), "dir_aliases")
    dir_ok = _dict(state.get("dir_ok", {}), "dir_ok")

    for p, values in meta.items():
        _safe_path(p, "file_meta")
        if not in_roots(p):
            raise ValueError(f"файл поза коренями сесії: {p}")
        if (not isinstance(values, list) or len(values) not in (3, 6)
                or not all(isinstance(v, int) and v >= 0 for v in values)):
            raise ValueError(f"некоректні метадані файла: {p}")
    if set(file_class) != set(meta):
        raise ValueError("file_class не відповідає file_meta")
    full_class = re.compile(r"^(0|[1-9][0-9]*):[0-9a-f]{64}$")
    unique_class = re.compile(r"^u:[1-9][0-9]*$")
    for p, cls in file_class.items():
        if not isinstance(cls, str) or not (
                full_class.fullmatch(cls) or unique_class.fullmatch(cls)):
            raise ValueError("некоректний клас файла")
        if full_class.fullmatch(cls) and int(cls.split(":", 1)[0]) != meta[p][0]:
            raise ValueError("розмір у класі не відповідає file_meta")
    for cls, size in class_size.items():
        if (not isinstance(cls, str) or not full_class.fullmatch(cls)
                or not isinstance(size, int) or size < 0
                or int(cls.split(":", 1)[0]) != size):
            raise ValueError("некоректний class_size")
    if class_paths is not None:
        if set(class_size) != set(class_paths):
            raise ValueError("class_size не відповідає class_paths")
        for cls, paths in class_paths.items():
            if not isinstance(cls, str) or not isinstance(paths, list):
                raise ValueError("некоректний class_paths")
            for p in paths:
                _safe_path(p, "class_paths")
                if p not in meta or file_class.get(p) != cls:
                    raise ValueError(
                        "class_paths посилається на невідомий файл")
    if dir_files is not None:
        for d, paths in dir_files.items():
            _safe_path(d, "dir_files")
            if not in_roots(d):
                raise ValueError(f"тека поза коренями сесії: {d}")
            if not isinstance(paths, list):
                raise ValueError("некоректний dir_files")
            for p in paths:
                _safe_path(p, "dir_files")
                if p not in meta or os.path.dirname(p) != d:
                    raise ValueError("файл виходить за межі своєї теки")
    for d, children in dir_children.items():
        _safe_path(d, "dir_children")
        if not in_roots(d):
            raise ValueError(f"тека поза коренями сесії: {d}")
        if not isinstance(children, list):
            raise ValueError("некоректний dir_children")
        for child in children:
            _safe_path(child, "dir_children")
            if child == d or os.path.dirname(child) != d:
                raise ValueError("дочірня тека виходить за межі батьківської")
    for d, links in dir_links.items():
        _safe_path(d, "dir_links")
        if not in_roots(d):
            raise ValueError(f"тека поза коренями сесії: {d}")
        if not isinstance(links, list):
            raise ValueError("некоректний dir_links")
        for item in links:
            if (not isinstance(item, list) or len(item) != 2
                    or not isinstance(item[1], str)):
                raise ValueError("некоректний symlink у сесії")
            p = _safe_path(item[0], "dir_links")
            if os.path.dirname(p) != d:
                raise ValueError("symlink виходить за межі своєї теки")
    for d, aliases in dir_aliases.items():
        _safe_path(d, "dir_aliases")
        if not in_roots(d):
            raise ValueError(f"тека поза коренями сесії: {d}")
        if not isinstance(aliases, list):
            raise ValueError("некоректний dir_aliases")
        for item in aliases:
            if not isinstance(item, list) or len(item) != 2:
                raise ValueError("некоректний hardlink у сесії")
            alias = _safe_path(item[0], "dir_aliases")
            canonical = _safe_path(item[1], "dir_aliases")
            if os.path.dirname(alias) != d or canonical not in meta:
                raise ValueError("hardlink посилається за межі маніфесту")
    for d, ok in dir_ok.items():
        _safe_path(d, "dir_ok")
        if not in_roots(d):
            raise ValueError(f"тека поза коренями сесії: {d}")
        if not isinstance(ok, bool):
            raise ValueError("некоректний dir_ok")
    # Опціональний розділ: старі сесії (v1/v2/v3) не мають dir_read_failed —
    # порожній список за замовчуванням лишається коректним.
    dir_read_failed = state.get("dir_read_failed", [])
    if not isinstance(dir_read_failed, list) or len(dir_read_failed) > _MAX_STATE_ITEMS:
        raise ValueError("некоректний dir_read_failed")
    for d in dir_read_failed:
        _safe_path(d, "dir_read_failed")
        if not in_roots(d):
            raise ValueError(f"тека поза коренями сесії: {d}")
    storage = _dict(state.get("file_storage", {}), "file_storage")
    for p, sid in storage.items():
        _safe_path(p, "file_storage")
        if not in_roots(p):
            raise ValueError(f"file_storage поза коренями: {p}")
        if (not isinstance(sid, list) or len(sid) != 2
                or not all(isinstance(v, int) and v >= 0 for v in sid)):
            raise ValueError(f"некоректний file_storage: {p}")
    alloc = _dict(state.get("file_alloc", {}), "file_alloc")
    for p, value in alloc.items():
        _safe_path(p, "file_alloc")
        if not in_roots(p):
            raise ValueError(f"file_alloc поза коренями: {p}")
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"некоректний file_alloc: {p}")
    ignored = state.get("ignored_pairs", [])
    if not isinstance(ignored, list) or len(ignored) > _MAX_STATE_ITEMS:
        raise ValueError("некоректний ignored_pairs")
    for pair in ignored:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("некоректна ігнорована пара")
        _safe_path(pair[0], "ignored_pairs")
        _safe_path(pair[1], "ignored_pairs")
        if not in_roots(pair[0]) or not in_roots(pair[1]):
            raise ValueError("ігнорована пара поза коренями сесії")
    return data


def _read_validated(path: str) -> dict:
    if os.path.getsize(path) > _MAX_SESSION_BYTES:
        raise ValueError("файл сесії завеликий")
    try:
        with open(path, "rb") as probe:
            compressed = probe.read(2) == b"\x1f\x8b"
        opener = gzip.open if compressed else open
        with opener(path, "rb") as fh:
            raw = fh.read(_MAX_SESSION_BYTES + 1)
    except (OSError, EOFError) as error:
        raise ValueError("пошкоджений файл сесії") from error
    if len(raw) > _MAX_SESSION_BYTES:
        raise ValueError("розпаковані дані сесії завеликі")
    try:
        return _validate_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("пошкоджений JSON сесії") from error


class _BoundedSessionReader:
    """File-like decompressed reader with byte cap, progress and cancellation."""

    def __init__(self, stream, path: str, cancel: threading.Event, progress):
        self._stream = stream
        self._path = path
        self._cancel = cancel
        self._progress = progress
        self._read = 0
        self._last_report = 0.0

    def read(self, size: int = -1) -> bytes:
        if self._cancel.is_set():
            raise OSError(errno.ECANCELED, "завантаження сесії скасовано", self._path)
        data = self._stream.read(size)
        self._read += len(data)
        if self._read > _MAX_SESSION_BYTES:
            raise ValueError("розпаковані дані сесії завеликі")
        now = time.monotonic()
        if (
            self._progress is not None
            and (not data or now - self._last_report >= 0.1)
        ):
            self._last_report = now
            self._progress("Читаю сесію", self._read, 0)
        return data


class _EventCursor:
    def __init__(self, events, path: str, cancel: threading.Event):
        self._events = iter(events)
        self._path = path
        self._cancel = cancel
        self._count = 0

    def next(self) -> tuple[str, object]:
        self._count += 1
        if self._count % 1024 == 0 and self._cancel.is_set():
            raise OSError(
                errno.ECANCELED, "завантаження сесії скасовано", self._path)
        try:
            return next(self._events)
        except StopIteration as error:
            raise ValueError("обірваний JSON сесії") from error

    def ensure_finished(self) -> None:
        try:
            next(self._events)
        except StopIteration:
            return
        raise ValueError("після JSON сесії знайдено зайві дані")


def _stream_value(
    cursor: _EventCursor,
    first: tuple[str, object],
    *,
    max_items: int = _MAX_STATE_ITEMS,
):
    event, value = first
    if event in {"string", "number", "boolean", "null"}:
        return value
    if event == "start_array":
        # Окрема назва від "out" у гілці start_map нижче —
        # той самий ідентифікатор для list і dict в одній функції змушував
        # mypy уніфікувати тип на всю функцію (list переміг, dict-гілка
        # ламалась). Гілки взаємовиключні за конструкцією; поведінка та сама.
        array_out: list = []
        while True:
            item = cursor.next()
            if item[0] == "end_array":
                return array_out
            if len(array_out) >= max_items:
                raise ValueError("завеликий масив у сесії")
            array_out.append(_stream_value(cursor, item, max_items=max_items))
    if event == "start_map":
        map_out: dict = {}
        while True:
            item_event, key = cursor.next()
            if item_event == "end_map":
                return map_out
            if item_event != "map_key" or not isinstance(key, str):
                raise ValueError("некоректний JSON-об’єкт сесії")
            if key in map_out:
                raise ValueError("повторений ключ JSON сесії")
            if len(map_out) >= max_items:
                raise ValueError("завеликий об’єкт у сесії")
            map_out[key] = _stream_value(
                cursor, cursor.next(), max_items=max_items)
    raise ValueError("некоректна JSON-подія сесії")


def _stream_object_entries(
    cursor: _EventCursor,
    first: tuple[str, object],
    consume,
) -> None:
    if first[0] != "start_map":
        raise ValueError("очікувався JSON-об’єкт сесії")
    seen: set[str] = set()
    while True:
        event, key = cursor.next()
        if event == "end_map":
            return
        if event != "map_key" or not isinstance(key, str):
            raise ValueError("некоректний JSON-об’єкт сесії")
        if key in seen:
            raise ValueError("повторений ключ JSON сесії")
        if len(seen) >= _MAX_STATE_ITEMS:
            raise ValueError("завеликий розділ сесії")
        seen.add(key)
        consume(key, _stream_value(cursor, cursor.next()))


def _cancel_checkpoint(
    cancel: threading.Event, path: str, index: int
) -> None:
    if index % 2048 == 0 and cancel.is_set():
        raise OSError(errno.ECANCELED, "завантаження сесії скасовано", path)


def _validate_streamed_result(
    res: core.ScanResult,
    roots: list[str],
    path: str,
    cancel: threading.Event,
) -> None:
    allowed_roots = _allowed_root_prefixes(roots)

    def in_roots(candidate: str) -> bool:
        return _in_allowed_roots(candidate, allowed_roots)

    if set(res.file_class) != set(res.file_meta):
        raise ValueError("file_class не відповідає file_meta")
    full_class = re.compile(r"^(0|[1-9][0-9]*):[0-9a-f]{64}$")
    unique_class = re.compile(r"^u:[1-9][0-9]*$")
    for index, (file_path, info) in enumerate(res.file_meta.items()):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(file_path, "file_meta")
        if not in_roots(file_path):
            raise ValueError(f"файл поза коренями сесії: {file_path}")
        class_id = res.file_class.get(file_path)
        if not isinstance(class_id, str) or not (
            full_class.fullmatch(class_id) or unique_class.fullmatch(class_id)
        ):
            raise ValueError("некоректний клас файла")
        if (
            full_class.fullmatch(class_id)
            and int(class_id.split(":", 1)[0]) != info.size
        ):
            raise ValueError("розмір у класі не відповідає file_meta")
    if set(res.class_size) != set(res.class_paths):
        raise ValueError("class_size не відповідає class_paths")
    for index, (class_id, size) in enumerate(res.class_size.items()):
        _cancel_checkpoint(cancel, path, index)
        if (
            not isinstance(class_id, str)
            or not full_class.fullmatch(class_id)
            or type(size) is not int
            or size < 0
            or int(class_id.split(":", 1)[0]) != size
        ):
            raise ValueError("некоректний class_size")
        paths = res.class_paths[class_id]
        if not isinstance(paths, list):
            raise ValueError("некоректний class_paths")
        for file_path in paths:
            _safe_path(file_path, "class_paths")
            if (
                file_path not in res.file_meta
                or res.file_class.get(file_path) != class_id
            ):
                raise ValueError("class_paths посилається на невідомий файл")
    for index, (directory, paths) in enumerate(res.dir_files.items()):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(directory, "dir_files")
        if not in_roots(directory) or not isinstance(paths, list):
            raise ValueError("некоректний dir_files")
        for file_path in paths:
            _safe_path(file_path, "dir_files")
            if (
                file_path not in res.file_meta
                or os.path.dirname(file_path) != directory
            ):
                raise ValueError("файл виходить за межі своєї теки")
    for index, (directory, children) in enumerate(res.dir_children.items()):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(directory, "dir_children")
        if not in_roots(directory) or not isinstance(children, list):
            raise ValueError("некоректний dir_children")
        for child in children:
            _safe_path(child, "dir_children")
            if child == directory or os.path.dirname(child) != directory:
                raise ValueError("дочірня тека виходить за межі батьківської")
    for index, (directory, links) in enumerate(res.dir_links.items()):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(directory, "dir_links")
        if not in_roots(directory) or not isinstance(links, list):
            raise ValueError("некоректний dir_links")
        for item in links:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[1], str)
            ):
                raise ValueError("некоректний symlink у сесії")
            link_path = _safe_path(item[0], "dir_links")
            if os.path.dirname(link_path) != directory:
                raise ValueError("symlink виходить за межі своєї теки")
    for index, (directory, aliases) in enumerate(res.dir_aliases.items()):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(directory, "dir_aliases")
        if not in_roots(directory) or not isinstance(aliases, list):
            raise ValueError("некоректний dir_aliases")
        for item in aliases:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("некоректний hardlink у сесії")
            alias = _safe_path(item[0], "dir_aliases")
            canonical = _safe_path(item[1], "dir_aliases")
            if os.path.dirname(alias) != directory or canonical not in res.file_meta:
                raise ValueError("hardlink посилається за межі маніфесту")
    for index, (directory, ok) in enumerate(res.dir_ok.items()):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(directory, "dir_ok")
        if not in_roots(directory) or not isinstance(ok, bool):
            raise ValueError("некоректний dir_ok")
    for index, directory in enumerate(res.dir_read_failed):
        _cancel_checkpoint(cancel, path, index)
        _safe_path(directory, "dir_read_failed")
        if not in_roots(directory):
            raise ValueError(f"тека поза коренями сесії: {directory}")
    for pair in res.ignored_pairs:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("некоректна ігнорована пара")
        _safe_path(pair[0], "ignored_pairs")
        _safe_path(pair[1], "ignored_pairs")
        if not in_roots(pair[0]) or not in_roots(pair[1]):
            raise ValueError("ігнорована пара поза коренями сесії")
    if cancel.is_set():
        raise OSError(errno.ECANCELED, "завантаження сесії скасовано", path)


def _load_session_streaming(
    path: str,
    cancel: threading.Event,
    progress,
    *,
    aggregate: bool = True,
    return_meta: bool = False,
):
    if os.path.getsize(path) > _MAX_SESSION_BYTES:
        raise ValueError("файл сесії завеликий")
    try:
        with open(path, "rb") as probe:
            compressed = probe.read(2) == b"\x1f\x8b"
        opener = gzip.open if compressed else open
        with opener(path, "rb") as stream:
            reader = _BoundedSessionReader(stream, path, cancel, progress)
            cursor = _EventCursor(ijson.basic_parse(reader), path, cancel)
            if cursor.next()[0] != "start_map":
                raise ValueError("корінь сесії має бути JSON-об’єктом")
            headers: dict[str, object] = {}
            result = core.ScanResult()
            result.live = False
            top_seen: set[str] = set()
            state_sections: set[str] = set()
            state_seen = False
            # Пул рядків: JSON-парсер створює окремий об'єкт на кожну появу
            # того самого шляху/класу; на сотнях тисяч файлів це десятки
            # мегабайтів дублікатів. Пул живе лише до кінця load.
            string_pool: dict[str, str] = {}

            def intern_str(value):
                if type(value) is str:
                    return string_pool.setdefault(value, value)
                return value

            def intern_list(value):
                if isinstance(value, list):
                    return [intern_str(item) for item in value]
                return value

            def consume_state(section: str, first: tuple[str, object]) -> None:
                nonlocal state_seen
                state_seen = True
                if section == "file_meta":
                    def consume_meta(file_path: str, values) -> None:
                        if (
                            not isinstance(values, list)
                            or len(values) not in (3, 6)
                            or not all(
                                type(value) is int and value >= 0
                                for value in values
                            )
                        ):
                            raise ValueError(
                                f"некоректні метадані файла: {file_path}")
                        extra = values[3:] if len(values) == 6 else [0, 0, 0]
                        file_path = intern_str(file_path)
                        result.file_meta[file_path] = core.FileInfo(
                            file_path, values[0], values[1], values[2], *extra)

                    _stream_object_entries(cursor, first, consume_meta)
                elif section == "file_class":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.file_class.__setitem__(
                            intern_str(key), intern_str(value)))
                elif section == "class_size":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.class_size.__setitem__(
                            intern_str(key), value))
                elif section == "class_paths":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.class_paths.__setitem__(
                            intern_str(key), intern_list(value)))
                elif section == "dir_files":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.dir_files.__setitem__(
                            intern_str(key), intern_list(value)))
                elif section == "dir_children":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.dir_children.__setitem__(
                            intern_str(key), intern_list(value)))
                elif section == "dir_links":
                    _stream_object_entries(
                        cursor,
                        first,
                        lambda key, value: result.dir_links.__setitem__(
                            intern_str(key),
                            [tuple(intern_list(item))
                             if isinstance(item, list) else item
                             for item in value]
                            if isinstance(value, list)
                            else value,
                        ),
                    )
                elif section == "dir_aliases":
                    _stream_object_entries(
                        cursor,
                        first,
                        lambda key, value: result.dir_aliases.__setitem__(
                            intern_str(key),
                            [tuple(intern_list(item))
                             if isinstance(item, list) else item
                             for item in value]
                            if isinstance(value, list)
                            else value,
                        ),
                    )
                elif section == "dir_ok":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.dir_ok.__setitem__(
                            intern_str(key), value))
                elif section == "file_storage":
                    _stream_object_entries(
                        cursor,
                        first,
                        lambda key, value: result.file_storage.__setitem__(
                            intern_str(key),
                            (value[0], value[1])
                            if isinstance(value, list) and len(value) == 2
                            else value,
                        ),
                    )
                elif section == "file_alloc":
                    _stream_object_entries(
                        cursor, first,
                        lambda key, value: result.file_alloc.__setitem__(
                            intern_str(key), value))
                elif section == "dir_read_failed":
                    values = _stream_value(cursor, first)
                    if not isinstance(values, list):
                        raise ValueError("некоректний dir_read_failed")
                    failed: set[str] = set()
                    for item in values:
                        if not isinstance(item, str):
                            raise ValueError("некоректний dir_read_failed")
                        failed.add(intern_str(item))
                    result.dir_read_failed = failed
                elif section == "ignored_pairs":
                    values = _stream_value(cursor, first)
                    if not isinstance(values, list):
                        raise ValueError("некоректний ignored_pairs")
                    ignored_pairs: set[tuple[str, str]] = set()
                    for item in values:
                        if (
                            not isinstance(item, list)
                            or len(item) != 2
                            or not all(isinstance(value, str) for value in item)
                        ):
                            raise ValueError("некоректна ігнорована пара")
                        ignored_pairs.add(
                            (intern_str(item[0]), intern_str(item[1])))
                    result.ignored_pairs = ignored_pairs
                else:
                    raise ValueError(f"невідомий розділ state: {section}")

            while True:
                event, key = cursor.next()
                if event == "end_map":
                    break
                if event != "map_key" or not isinstance(key, str):
                    raise ValueError("некоректний JSON-об’єкт сесії")
                if key in top_seen:
                    raise ValueError("повторений ключ JSON сесії")
                top_seen.add(key)
                first = cursor.next()
                if key == "state":
                    if first[0] != "start_map":
                        raise ValueError("некоректний розділ state")
                    while True:
                        state_event, section = cursor.next()
                        if state_event == "end_map":
                            break
                        if (
                            state_event != "map_key"
                            or not isinstance(section, str)
                            or section in state_sections
                        ):
                            raise ValueError("некоректний або повторений state key")
                        state_sections.add(section)
                        consume_state(section, cursor.next())
                elif key in {
                    "version", "created_ns", "roots", "partial",
                    "files_seen", "bytes_seen", "errors_total", "counts", "errors",
                }:
                    headers[key] = _stream_value(cursor, first)
                else:
                    raise ValueError(f"невідомий ключ сесії: {key}")
            cursor.ensure_finished()
    except OSError:
        raise
    except (
        EOFError,
        ijson.JSONError,
        UnicodeError,
        RecursionError,
        TypeError,
    ) as error:
        raise ValueError("пошкоджений JSON сесії") from error

    if not state_seen or headers.get("version") not in _SUPPORTED_SESSION_VERSIONS:
        raise ValueError(_unsupported_version_message(headers.get("version")))
    if type(headers.get("created_ns")) is not int:
        raise ValueError("у сесії немає коректного created_ns")
    roots = headers.get("roots", [])
    if not isinstance(roots, list) or len(roots) > 10_000:
        raise ValueError("некоректний список коренів")
    for root in roots:
        _safe_path(root, "roots")
    errors = headers.get("errors", [])
    errors_total = headers.get(
        "errors_total", len(errors) if isinstance(errors, list) else 0)
    if (
        not isinstance(errors, list)
        or len(errors) > core.MAX_ERROR_DETAILS
        or type(errors_total) is not int
        or errors_total < len(errors)
        or errors_total > _MAX_STATE_ITEMS
        or any(
            not isinstance(message, str) or len(message) > 32768
            for message in errors
        )
    ):
        raise ValueError("некоректний список помилок сесії")
    files_seen = headers.get("files_seen", len(result.file_meta))
    bytes_seen = headers.get(
        "bytes_seen", sum(info.size for info in result.file_meta.values()))
    partial = headers.get("partial", False)
    counts = headers.get("counts", {})
    if (
        type(files_seen) is not int
        or files_seen < 0
        or type(bytes_seen) is not int
        or bytes_seen < 0
        or not isinstance(partial, bool)
        or not isinstance(counts, dict)
        or set(counts) - {"files", "dirs", "pairs"}
        or any(
            type(counts.get(name, 0)) is not int
            or counts.get(name, 0) < 0
            for name in ("files", "dirs", "pairs")
        )
    ):
        raise ValueError("некоректна статистика сесії")
    result.files_seen = files_seen
    result.bytes_seen = bytes_seen
    result.errors = list(errors)
    result.errors_total = max(len(errors), errors_total)
    result.partial = partial
    if headers.get("version") == 3:
        # v3 не містить похідних розділів — відновлюємо їх ПЕРЕД повною
        # валідацією, щоб перехресні перевірки виконувались як для v2.
        # У v1/v2 відсутність розділів лишається помилкою (строгість
        # старого формату не послаблюється).
        if "class_size" not in state_sections:
            restored_sizes: dict[str, int] = {}
            for class_id in result.file_class.values():
                if (isinstance(class_id, str)
                        and class_id not in restored_sizes):
                    prefix, sep, _digest = class_id.partition(":")
                    if sep and prefix.isdigit():
                        restored_sizes[class_id] = int(prefix)
            result.class_size = restored_sizes
        if "class_paths" not in state_sections:
            restored: dict[str, list[str]] = {}
            for file_path, class_id in result.file_class.items():
                if isinstance(class_id, str) and class_id in result.class_size:
                    restored.setdefault(class_id, []).append(file_path)
            for paths in restored.values():
                paths.sort()
            for class_id in result.class_size:
                restored.setdefault(class_id, [])
            result.class_paths = restored
        if "dir_files" not in state_sections:
            files_by_dir: dict[str, list[str]] = {
                directory: [] for directory in result.dir_ok}
            for file_path in result.file_meta:
                files_by_dir.setdefault(
                    os.path.dirname(file_path), []).append(file_path)
            result.dir_files = files_by_dir
    if progress is not None:
        progress("Перевіряю структуру сесії", 0, 0)
    _validate_streamed_result(result, roots, path, cancel)
    if aggregate:
        if progress is not None:
            progress("Групую результати сесії", 0, 0)
        core._aggregate(result, cancel=cancel)
    if cancel.is_set():
        raise OSError(errno.ECANCELED, "завантаження сесії скасовано", path)
    if return_meta:
        meta = {
            "version": headers["version"],
            "created_ns": headers["created_ns"],
            "roots": list(roots),
            "partial": partial,
            "files_seen": files_seen,
            "bytes_seen": bytes_seen,
            "errors_total": result.errors_total,
            "counts": {
                name: counts.get(name, 0)
                for name in ("files", "dirs", "pairs")
            },
        }
        return result, meta
    return result


def load_session(
    path: str,
    *,
    cancel: threading.Event | None = None,
    progress=None,
) -> core.ScanResult:
    """Відновлює ScanResult зі знімка і перебудовує groups/pairs через
    core._aggregate() (сам знімок groups не містить)."""
    cancel = cancel or threading.Event()
    return _load_session_streaming(path, cancel, progress)


def delete_session(path: str) -> None:
    name = os.path.basename(path)
    stem = (
        name[: -len(".json.gz")] if name.endswith(".json.gz")
        else name[: -len(".json")] if name.endswith(".json")
        else ""
    )
    if (os.path.basename(os.path.dirname(path)) != "sessions"
            or not stem.isdigit()):
        raise ValueError("небезпечний шлях сесії")
    for victim in (path, _meta_path(path)):
        try:
            os.remove(victim)
        except OSError:
            pass


def export_session(path: str, dst: str) -> str:
    """Копіює payload сесії у файл користувача (мета вбудована — файл
    самодостатній для import_session на будь-якій машині)."""
    _load_session_streaming(
        path,
        threading.Event(),
        None,
        aggregate=False,
    )
    _atomic_copy(path, dst)
    return dst


def import_session(src: str, base_dir: str | None = None) -> str:
    """Валідує файл сесії і кладе його в історію (payload + сайдкар).
    ValueError — якщо файл не схожий на сесію DupScan."""
    # Валідація й читання src — ПОЗА локом (довге); лише
    # публікація+prune — критична секція, як і в save_session/
    # compact_store. import_session мав
    # той самий вразливий патерн (_write_meta/_atomic_copy/_prune_old без
    # синхронізації), той самий лок закривав скрізь, окрім тут.
    _result, meta = _load_session_streaming(
        src,
        threading.Event(),
        None,
        aggregate=False,
        return_meta=True,
    )
    with open(src, "rb") as probe:
        suffix = ".json.gz" if probe.read(2) == b"\x1f\x8b" else ".json"
    path = ""
    meta_path = ""
    try:
        with _locked(base_dir) as sdir:
            path = os.path.join(sdir, f"{time.time_ns()}{suffix}")
            meta_path = _meta_path(path)
            # As in save_session, the payload is the final publication boundary.
            _write_meta(path, meta)
            _atomic_copy(src, path)
            _prune_old(sdir)
        return path
    except Exception:
        for victim in (path, meta_path):
            if victim:
                try:
                    os.remove(victim)
                except OSError:
                    pass
        raise
