"""Файлові операції DupScan: Кошик з доказами, перевірені move/copy,
Кошик цілої теки після Merkle-звірки.

Винесено з app.py (2.17 «Solid Core») БЕЗ зміни поведінки. Модуль не
залежить від Qt і від app; точки перехоплення (to_trash) приймаються
параметром trash — app передає свій глобал, тому monkeypatch
app.to_trash у тестах та інтеграціях працює як раніше.

НЕПОРУШНІ інваріанти безпеки: видалення лише в Кошик; перед деструктивом —
свіже повне читання жертви І незалежної копії; fail-closed на будь-якій
зміні; жодних перезаписів цілі.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import stat as stat_mod
import threading
import time
import unicodedata

import blake3

import dupscan.domain.core as core
import dupscan.infra.preferences as preferences
import dupscan.infra.removal_history as removal_history

_TRASH_CONTEXT = threading.local()


def _trash_identity(st: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Identity proving the same path object is still authorized for Trash."""
    return (
        int(st.st_dev),
        int(st.st_ino),
        int(stat_mod.S_IFMT(st.st_mode)),
        int(st.st_size),
        int(st.st_mtime_ns),
        int(st.st_ctime_ns),
    )


def _directory_identity_from_stat(st: os.stat_result) -> tuple[int, int]:
    if not stat_mod.S_ISDIR(st.st_mode) or stat_mod.S_ISLNK(st.st_mode):
        raise OSError(errno.ENOTDIR, "очікувалась звичайна тека")
    return int(st.st_dev), int(st.st_ino)


def _directory_identity(path: str) -> tuple[int, int]:
    return _directory_identity_from_stat(os.lstat(path))


TRASH_WORKERS = 8  # виміряно: 2.5x на зовнішньому exFAT
TRASH_PARALLEL_THRESHOLD = 8  # менші партії й так миттєві


def to_trash(
    paths: list[str],
    *,
    expected_identities: dict[str, tuple[int, int, int, int, int, int]]
    | None = None,
) -> list[str]:
    """Move paths to Trash and append a best-effort auditable history entry.

    Send2Trash does not return the resulting name. On macOS we conservatively
    locate it by the exact device/inode in the expected Trash root. If that
    cannot be proven, history is still written and Finder remains the restore
    fallback; no filename guess is ever recorded.
    """
    import send2trash
    errs: list[str] = []
    # Останній рубіж теки-еталона (2.23.0): навіть якщо всі
    # верхні шари помилились, захищений шлях далі цієї лінії не проходить.
    # Зіпсований список еталонів = fail-closed для ВСІХ шляхів: захист не
    # можна тихо втратити через биту конфігурацію.
    try:
        reference_roots = preferences.load_reference_roots()
    except preferences.PreferencesError as error:
        return [
            f"{path}: список тек-еталонів не читається ({error}) — "
            "видалення зупинено" for path in paths
        ]
    snapshots: dict[str, tuple[os.stat_result, list[str]]] = {}
    authorized: dict[str, tuple[int, int, int, int, int, int]] = {}
    normalized_paths = []
    for path in dict.fromkeys(
            os.path.normpath(os.path.abspath(p)) for p in paths):
        if preferences.is_protected(path, reference_roots):
            errs.append(f"{path}: шлях під текою-еталоном — недоторканний")
            continue
        normalized_paths.append(path)
    normalized_expected = (
        {
            os.path.normpath(os.path.abspath(path)): identity
            for path, identity in expected_identities.items()
        }
        if expected_identities is not None
        else None
    )

    def trash_roots(path: str, st: os.stat_result) -> list[str]:
        roots: list[str] = []
        home_trash = os.path.expanduser("~/.Trash")
        try:
            if os.stat(os.path.expanduser("~")).st_dev == st.st_dev:
                roots.append(home_trash)
        except OSError:
            pass
        current = os.path.dirname(os.path.abspath(path))
        while not os.path.ismount(current):
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        volume_trash = os.path.join(current, ".Trashes", str(os.getuid()))
        if volume_trash not in roots:
            roots.append(volume_trash)
        return roots

    def locate(st: os.stat_result, roots: list[str], original: str) -> str | None:
        name = os.path.basename(original)
        stem, extension = os.path.splitext(name)
        for root in roots:
            likely = [os.path.join(root, name)]
            for number in range(2, 101):
                likely.append(os.path.join(root, f"{stem} {number}{extension}"))
            for candidate in likely:
                try:
                    current = os.lstat(candidate)
                except OSError:
                    continue
                if (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                    return os.path.normpath(os.path.abspath(candidate))
        inspected = 0
        for root in roots:
            try:
                with os.scandir(root) as entries:
                    for entry in entries:
                        inspected += 1
                        if inspected > 200_000:
                            return None
                        try:
                            current = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        if (current.st_dev, current.st_ino) == (st.st_dev, st.st_ino):
                            return os.path.normpath(os.path.abspath(entry.path))
            except OSError:
                continue
        return None

    for path in normalized_paths:
        try:
            before = os.lstat(path)
            current_identity = _trash_identity(before)
            if normalized_expected is not None:
                expected = normalized_expected.get(path)
                if expected is None:
                    raise OSError(
                        errno.ESTALE,
                        "немає identity, що дозволяє системний Кошик",
                        path,
                    )
                if current_identity != expected:
                    raise OSError(
                        errno.ESTALE,
                        "об’єкт змінився після повної перевірки",
                        path,
                    )
                authorized[path] = expected
            else:
                authorized[path] = current_identity
            snapshots[path] = (before, trash_roots(path, before))
        except OSError as error:
            errs.append(f"{path}: {error}")
    ready = [(path, authorized[path]) for path in normalized_paths
             if authorized.get(path) is not None]

    def move_one(item) -> str | None:
        path, expected = item
        try:
            # send2trash exposes a path-only API. Keep this identity check
            # immediately adjacent and fail closed if another process replaced
            # the object after BLAKE3/Merkle verification.
            if _trash_identity(os.lstat(path)) != expected:
                raise OSError(
                    errno.ESTALE,
                    "об’єкт замінено безпосередньо перед системним Кошиком",
                    path,
                )
            send2trash.send2trash(path)
        except Exception as e:  # noqa: BLE001
            return f"{path}: {e}"
        return None

    # Один виклик Кошика коштує ~22 мс на зовнішньому exFAT (виміряно
    # scripts/trash_bench.py), тому на великих партіях виклики йдуть пулом:
    # 46 -> 114 файлів/с. Перевірка тотожності лишається ВСЕРЕДИНІ воркера,
    # впритул до виклику; журнал пишеться потім у цьому потоці.
    if len(ready) >= TRASH_PARALLEL_THRESHOLD:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=TRASH_WORKERS) as pool:
            # Перейменовано з "error", щоб не збігатися з
            # `except OSError as error` вище в тій самій функції — те саме
            # ім'я поза except-блоком і плутало mypy [misc], і згодом
            # каскадом ламало висновок типу для "item" нижче.
            for move_err in pool.map(move_one, ready):  # порядок збережено
                if move_err is not None:
                    errs.append(move_err)
    else:
        for queued in ready:
            move_err = move_one(queued)
            if move_err is not None:
                errs.append(move_err)
    if snapshots:
        failed = _failed_trash_paths(errs, list(snapshots))
        items = []
        for path, (before, roots) in snapshots.items():
            mode = before.st_mode
            kind = ("symlink" if stat_mod.S_ISLNK(mode) else
                    "directory" if stat_mod.S_ISDIR(mode) else "file")
            item = {
                "original_path": path,
                "size": max(0, int(before.st_size)),
                "kind": kind,
                "status": "failed" if path in failed else "trashed",
            }
            if path in failed:
                item["error"] = next(
                    (error[:4096] for error in errs
                     if error == path or error.startswith(path + ": ")), "")
            else:
                destination = locate(before, roots, path)
                if destination:
                    item["trashed_path"] = destination
                    try:
                        destination_info = os.lstat(destination)
                        if (
                            destination_info.st_dev,
                            destination_info.st_ino,
                        ) == (before.st_dev, before.st_ino):
                            item["identity"] = list(
                                _trash_identity(destination_info))
                    except OSError:
                        # Without a proven identity the audit entry remains
                        # useful, but conservative automatic restore is
                        # unavailable and Finder is the fallback.
                        pass
                digest = getattr(_TRASH_CONTEXT, "digests", {}).get(path)
                if (isinstance(digest, str) and len(digest) == 64
                        and all(char in "0123456789abcdef" for char in digest)):
                    item["digest"] = digest
            items.append(item)
        try:
            succeeded = len(items) - len(failed)
            status = ("completed" if succeeded == len(items) else
                      "failed" if succeeded == 0 else "partial")
            removal_history.append_operation(
                items, status=status, errors=(error[:4096] for error in errs[:100]))
        except Exception:  # noqa: BLE001 — audit failure cannot undo a safe Trash move
            pass
    return errs


def _failed_trash_paths(errors: list[str], candidates: list[str]) -> set[str]:
    """Витягнути шлях без помилки split(':') для легальних ':' у назві."""
    return {path for path in candidates
            if any(error == path or error.startswith(path + ": ") for error in errors)}


def _same_device(a: str, b: str) -> bool:
    """Один том? Крос-томний перенос v1 не робимо (copy+delete ризикований)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _contained(root: str, path: str) -> bool:
    try:
        root = os.path.abspath(root)
        path = os.path.abspath(path)
        return os.path.commonpath((root, path)) == root
    except (OSError, ValueError):
        return False


def _class_digest(class_id: str | None) -> str | None:
    if not class_id or class_id.startswith("u:") or ":" not in class_id:
        return None
    return class_id.split(":", 1)[1]


def _to_trash_known(
    res,
    paths: list[str],
    *,
    expected_identities: dict[str, tuple[int, int, int, int, int, int]]
    | None = None,
    trash=None,
) -> list[str]:
    """Attach already-proven BLAKE3 identities to the audit history.

    trash — точка перехоплення Кошика (app передає свій глобал to_trash,
    щоб підміна app.to_trash діяла); None — власний to_trash модуля.
    """
    trash = trash if trash is not None else to_trash
    previous = getattr(_TRASH_CONTEXT, "digests", None)
    _TRASH_CONTEXT.digests = {
        os.path.normpath(os.path.abspath(path)):
        _class_digest(res.file_class.get(path))
        for path in paths
    }
    try:
        return trash(paths, expected_identities=expected_identities)
    finally:
        if previous is None:
            try:
                del _TRASH_CONTEXT.digests
            except AttributeError:
                pass
        else:
            _TRASH_CONTEXT.digests = previous


def _paranoid_verification() -> bool:
    """Чи власник вимагає повного перечитування навіть за чинного доказу."""
    if getattr(core, "PARANOID_VERIFICATION", False):
        return True
    getter = getattr(preferences, "paranoid_verification", None)
    if callable(getter):
        try:
            return bool(getter())
        except Exception:  # noqa: BLE001 — налаштування не мають валити операцію
            return False
    return False


def _proven_stat(
    res, path: str, expected_digest: str,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
) -> os.stat_result | None:
    """Довести, що *path* ЗАРАЗ має вміст свого класу — найдешевшим шляхом.

    Скан формує групи виключно з повних BLAKE3, тому доказ уже існує. Якщо
    метадані файла не змінилися з моменту хешування, вміст той самий і читати
    нічого не потрібно. Читання відбувається лише коли доказ слабший за
    повний, метадані зсунулися, або власник обрав параноїдальний режим.
    """
    fi = res.file_meta.get(path)
    if fi is None:
        return None
    try:
        current = os.lstat(path)
    except OSError:
        return None
    if not stat_mod.S_ISREG(current.st_mode):
        return None
    if (core.proof_kind(res, path) == core.FULL_PROOF
            and core.proof_is_current(fi, current)
            and not _paranoid_verification()
            and _fs_trusts_metadata(path)):
        return current  # нуль читань: доказ чинний, ФС гідна довіри
    # Доказ треба підтвердити або підвищити: свіже повне читання. expected=None,
    # бо метадані вже зсунулися; TOCTOU-замок усередині _hash_file лишається.
    digest, fresh = core.verify_current_file(
        path, None, cancel=cancel, pause=pause, progress=progress)
    if digest != expected_digest:
        return None
    return fresh


def _verified_survivor(
    res, victim: str, removal: set[str],
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
    survivor_cache: dict | None = None,
) -> os.stat_result | None:
    """Довести жертву і наявність незалежної живої копії її класу.

    Копія доводиться ОДИН раз на клас: survivor_cache тримає результат між
    жертвами однієї операції.
    """
    cls = res.file_class.get(victim)
    expected_digest = _class_digest(cls)
    if expected_digest is None:
        return None
    vst = _proven_stat(res, victim, expected_digest,
                       cancel=cancel, pause=pause, progress=progress)
    if vst is None:
        return None

    def removed(path: str) -> bool:
        return any(path == item or path.startswith(item.rstrip(os.sep) + os.sep)
                   for item in removal)

    cached = survivor_cache.get(cls) if survivor_cache is not None else None
    if cached is not None:
        survivor, sidentity = cached
        if (not removed(survivor)
                and sidentity != (vst.st_dev, vst.st_ino)
                and os.path.exists(survivor)):
            return vst
    for survivor in res.class_paths.get(cls, ()):
        if survivor == victim or removed(survivor):
            continue
        try:
            sst = _proven_stat(res, survivor, expected_digest,
                               cancel=cancel, pause=pause, progress=progress)
        except OSError:
            continue
        if sst is None:
            continue
        if (sst.st_dev, sst.st_ino) != (vst.st_dev, vst.st_ino):
            if survivor_cache is not None:
                survivor_cache[cls] = (survivor, (sst.st_dev, sst.st_ino))
            return vst
    return None


def _open_directory_root(
    root: str, expected_identity: tuple[int, int] | None = None
) -> int:
    """Open one real directory root and optionally bind it to earlier proof."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.path.abspath(root), flags)
    try:
        identity = _directory_identity_from_stat(os.fstat(descriptor))
        if expected_identity is not None and identity != expected_identity:
            raise OSError(
                errno.ESTALE,
                "кореневу теку замінено після підготовки операції",
                root,
            )
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _safe_source_parent(
    root: str,
    source_path: str,
    expected_root_identity: tuple[int, int] | None = None,
) -> tuple[int, str]:
    """Pin a source parent via openat without following any path symlink."""
    root = os.path.abspath(root)
    source_path = os.path.abspath(source_path)
    if not _contained(root, source_path) or source_path == root:
        raise OSError("джерело виходить за межі кореня")
    relative = os.path.relpath(source_path, root)
    parts = relative.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise OSError("некоректний компонент шляху джерела")
    current = _open_directory_root(root, expected_root_identity)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except Exception:
        os.close(current)
        raise


def _safe_destination_parent(
    root: str,
    parent_path: str,
    expected_root_identity: tuple[int, int] | None = None,
) -> int:
    """Create/open a destination parent without following symlink components.

    The returned directory fd pins the verified directory for exclusive
    publication even if a path component is renamed concurrently.
    """
    root = os.path.abspath(root)
    parent_path = os.path.abspath(parent_path)
    if not _contained(root, parent_path):
        raise OSError("цільовий шлях виходить за межі кореня")
    relative = os.path.relpath(parent_path, root)
    parts = [] if relative == "." else relative.split(os.sep)
    if any(part in ("", ".", "..") for part in parts):
        raise OSError("некоректний компонент цільового шляху")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    current = _open_directory_root(root, expected_root_identity)
    try:
        for part in parts:
            try:
                os.mkdir(part, mode=0o755, dir_fd=current)
            except FileExistsError:
                pass
            next_fd = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = next_fd
        return current
    except Exception:
        os.close(current)
        raise


def _operation_wait(
    cancel: threading.Event, pause: threading.Event | None
) -> bool:
    core._wait_if_paused(pause, cancel)
    return not cancel.is_set()


def _verified_to_trash_files(
        res, paths: list[str], trash=None, survivor=None) -> list[str]:
    """Остання перевірка і Кошик — в одному фоновому завданні.

    Жоден шлях не передається send2trash, якщо безпосередньо перед цим не
    перечитані повністю і він, і незалежна копія. trash/survivor — точки
    перехоплення (app передає свої глобали для monkeypatch-сумісності).
    """
    survivor = survivor if survivor is not None else _verified_survivor
    removal = set(paths)
    errors: list[str] = []
    survivor_cache: dict = {}  # клас -> доведена копія (один раз на клас)
    for path in paths:
        try:
            victim_stat = survivor(
                res, path, removal, survivor_cache=survivor_cache)
            if victim_stat is None:
                errors.append(f"{path}: повторна перевірка не пройдена")
                continue
            errors.extend(_to_trash_known(
                res,
                [path],
                expected_identities={path: _trash_identity(victim_stat)},
                trash=trash,
            ))
        except OSError as e:
            errors.append(f"{path}: {e}")
    return errors


# Помилки os.link, що означають «ця файлова система не вміє жорстких
# посилань» (exFAT/FAT/msdos/деякі SMB/NAS), а не тимчасовий збій. Для них
# публікація переходить на ексклюзивний rename-фолбек. EXDEV сюди НЕ
# входить: крос-девайсний перенос — окремий випадок, який rename не рятує,
# і його треба показувати як помилку, а не тихо обробляти.
_LINK_UNSUPPORTED = frozenset({
    errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EMLINK, errno.ENOSYS,
})

# Межа суфікс-retry « (N)» у _transfer_files: іменований, а не «голе»
# 10_000 у циклі — тест на вичерпання monkeypatch-ить
# це до малого числа, щоб не створювати 10 000 реальних колізій заради
# швидкості. Значення не змінює поведінку — той самий ліміт, що й раніше.
_MAX_SUFFIX_ATTEMPTS = 10_000


class _Attrlist(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_int64)]


_ATTR_BIT_MAP_COUNT = 5
_ATTR_CMN_CRTIME = 0x00000200
_FSOPT_NOFOLLOW = 0x00000001


def _libc():
    """libc з setattrlist; None там, де його немає (не-macOS, пісочниця)."""
    handle = getattr(_libc, "_handle", "unset")
    if handle == "unset":
        try:
            handle = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            handle.setattrlist.argtypes = [
                ctypes.c_char_p, ctypes.POINTER(_Attrlist),
                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong,
            ]
        except (OSError, AttributeError, TypeError):
            handle = None
        # Памʼять-на-функції — свідомий кеш-патерн (звичайна
        # функція як об'єкт), mypy не бачить динамічних атрибутів callable.
        _libc._handle = handle  # type: ignore[attr-defined]
    return handle


def _set_creation_time(path: str, birthtime_ns: int) -> bool:
    """Виставити дату створення (ATTR_CMN_CRTIME). Best-effort.

    Копія завжди народжується «зараз»: єдиний спосіб віддати їй дату
    створення джерела — setattrlist. Працює і на APFS, і на exFAT, тобто
    покриває злиття МІЖ РІЗНИМИ томами, де переносу не існує і копія —
    єдиний шлях.

    FSOPT_NOFOLLOW: підміна опублікованого імені на symlink не має
    перекинути запис мітки на чужий файл.

    Повертає True при успіху. Провал НЕ вважається помилкою переносу:
    файл уже скопійовано й побайтово звірено, і втрачена мітка не привід
    оголошувати верифіковану копію невдалою.
    """
    lib = _libc()
    if lib is None or birthtime_ns <= 0:
        return False
    attributes = _Attrlist(
        _ATTR_BIT_MAP_COUNT, 0, _ATTR_CMN_CRTIME, 0, 0, 0, 0)
    value = _Timespec(
        birthtime_ns // 1_000_000_000, birthtime_ns % 1_000_000_000)
    try:
        return lib.setattrlist(
            os.fsencode(path), ctypes.byref(attributes),
            ctypes.byref(value), ctypes.sizeof(value), _FSOPT_NOFOLLOW) == 0
    except OSError:
        return False


# ---- D3: автопараноя за файловою системою ----------------------------------
#
# struct statfs з <sys/mount.h> (64-бітна inode-версія, чинна з 10.6);
# офсети звірені емпірично на цій машині (APFS -> b"apfs", змонтований
# exFAT-образ -> b"exfat") перед тим, як покладатись на них у гейті.

_MFSTYPENAMELEN = 16
_MAXPATHLEN = 1024


class _Fsid(ctypes.Structure):
    _fields_ = [("val", ctypes.c_int32 * 2)]


class _Statfs(ctypes.Structure):
    _fields_ = [
        ("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64), ("f_fsid", _Fsid),
        ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * _MFSTYPENAMELEN),
        ("f_mntonname", ctypes.c_char * _MAXPATHLEN),
        ("f_mntfromname", ctypes.c_char * _MAXPATHLEN),
        ("f_flags_ext", ctypes.c_uint32), ("f_reserved", ctypes.c_uint32 * 7),
    ]


def _statfs_libc():
    """Окремий кеш-хендл від _libc(): інший набір argtypes на тому самому дескрипторі."""
    handle = getattr(_statfs_libc, "_handle", "unset")
    if handle == "unset":
        try:
            handle = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            handle.statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_Statfs)]
            handle.statfs.restype = ctypes.c_int
        except (OSError, AttributeError, TypeError):
            handle = None
        # Той самий патерн, що й _libc() вище.
        _statfs_libc._handle = handle  # type: ignore[attr-defined]
    return handle


def _statfs_fstype(path: str) -> str | None:
    """Ім'я файлової системи (lower-case) під *path*, або None при помилці.

    Немає читання файла — лише statfs(2) на шляху. None охоплює і не-macOS,
    і пісочницю без libc, і будь-яку помилку самого виклику — усе це
    зводиться до fail-closed у _fs_trusts_metadata (невідома ФС -> не
    довіряти метаданим).
    """
    lib = _statfs_libc()
    if lib is None:
        return None
    buf = _Statfs()
    try:
        rc = lib.statfs(os.fsencode(path), ctypes.byref(buf))
    except (OSError, TypeError, ValueError):
        return None
    if rc != 0:
        return None
    return buf.f_fstypename.decode("utf-8", "replace").lower()


_FS_METADATA_WHITELIST = frozenset({"apfs", "hfs"})
_fs_trust_cache: dict[int, bool] = {}  # st_dev -> довіряти metadata-shortcut


def _fs_trusts_metadata(path: str) -> bool:
    """Чи можна довіряти metadata-shortcut на ФС, де лежить *path*.

    exFAT: mtime — крок 10 мс, ctime несправжній —
    підміна вмісту з тим самим розміром у цьому вікні непомітна для
    identity. Довірений whitelist — лише {"apfs", "hfs"}: усе інше
    (exfat, msdos, smbfs, nfs, webdav, невідомий тип, помилка statfs) ->
    False, fail-closed до повного перечитування. Кеш за st_dev: тип ФС
    тому не змінюється, поки він змонтований, а statfs — зайвий syscall
    на кожен файл гейта інакше.
    """
    try:
        st_dev = os.stat(path).st_dev
    except OSError:
        return False
    cached = _fs_trust_cache.get(st_dev)
    if cached is not None:
        return cached
    trusted = _statfs_fstype(path) in _FS_METADATA_WHITELIST
    _fs_trust_cache[st_dev] = trusted
    return trusted


def _source_birthtime_ns(source_stat: os.stat_result) -> int:
    value = getattr(source_stat, "st_birthtime_ns", None)
    if value:
        return int(value)
    return int(getattr(source_stat, "st_birthtime", 0) * 1e9)


def _apply_source_dates(
    dest_name: str, dest_dir_fd: int, dest_path: str,
    source_stat: os.stat_result,
) -> None:
    """Перенести дати джерела на ОПУБЛІКОВАНИЙ файл.

    Саме після публікації, а не на дескрипторі до неї: `os.fdopen` буферизує,
    і `flush`/`close` після `utime` знову підіймали mtime на «зараз» — файл
    втрачав дату зміни (помічено на exFAT диску власника, але дефект був на
    будь-якій ФС, бо причина — порядок відносно скидання буфера).
    """
    try:
        os.utime(
            dest_name,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            dir_fd=dest_dir_fd, follow_symlinks=False,
        )
    except (OSError, NotImplementedError):
        pass
    _set_creation_time(dest_path, _source_birthtime_ns(source_stat))


def _destination_holds_same_content(
    dest_name: str, dest_dir_fd: int, digest: str,
    source_stat: os.stat_result, is_symlink: bool,
) -> bool:
    """Чи лежить у цілі під цим іменем РІВНО той самий вміст.

    Потрібно, бо план може назвати файл унікальним помилково: коли знімок
    неповний (сторона A має непрочитані шляхи), файли B виглядають
    відсутніми в A. Копія тоді створювала ` (2)`-дублікат — множила дані
    замість дедуплікації.

    Порівняння тільки за вмістом: збіг імені й розміру нічого не доводить,
    тому читається й хешується повністю. Будь-яка неоднозначність (немає
    доступу, інший тип, гонитва) → False, тобто звичайний суфікс-retry.
    Інваріант «ціль ніколи не перезаписується» цим не послаблюється: ця
    гілка нічого не пише, вона лише пропускає зайву роботу.
    """
    try:
        if is_symlink:
            target = os.readlink(dest_name, dir_fd=dest_dir_fd)
            fresh = blake3.blake3(
                target.encode("utf-8", "surrogatepass")).hexdigest()
            return fresh == digest
        descriptor = os.open(
            dest_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=dest_dir_fd)
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "rb", buffering=0) as existing:
            current = os.fstat(existing.fileno())
            if (not stat_mod.S_ISREG(current.st_mode)
                    or current.st_size != source_stat.st_size):
                return False
            hasher = blake3.blake3(
                max_threads=core._blake3_threads(current.st_size, True))
            while True:
                chunk = existing.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest() == digest
    except OSError:
        return False


def _fresh_digest(
    path: str,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
) -> tuple[str, os.stat_result]:
    """Повне читання файла з диска: (BLAKE3, stat). O_NOFOLLOW."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        current = os.fstat(handle.fileno())
        if not stat_mod.S_ISREG(current.st_mode):
            raise OSError(errno.EINVAL, "не звичайний файл", path)
        hasher = blake3.blake3(
            max_threads=core._blake3_threads(current.st_size, True))
        while True:
            if cancel is not None and not _operation_wait(cancel, pause):
                raise OSError(errno.ECANCELED, "скасовано", path)
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest(), current


def _survivor_by_content(
    victim: str, claimed: str | None, removal: set[str],
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
) -> bool:
    """Свіжий доказ живої копії для файла БЕЗ класу в скані.

    Потрібен для неохоплених профілем файлів (`__pycache__` та інші
    EXCLUDE_NAMES): у них ніколи не буде `res.file_class`, тому звичайний
    `_verified_survivor` їх не рятує — раніше саме тому злиття мусило
    ПЕРЕНОСИТИ такий файл у ціль, навіть коли ідентичний уже лежав там.

    Пропуск зберігає, ДЕ лежить доведена копія; тут це твердження
    перевіряється заново з диска, безпосередньо перед Кошиком. Тобто
    контракт «свіже повне читання жертви І незалежної копії» тримається —
    доказ з часу злиття не приймається на віру.

    Fail-closed: будь-яка неоднозначність → False.
    """
    if not claimed:
        return False
    if any(claimed == item or claimed.startswith(item.rstrip(os.sep) + os.sep)
           for item in removal):
        return False  # «копія» всередині того, що їде в Кошик — не копія
    try:
        victim_digest, victim_stat = _fresh_digest(victim, cancel, pause)
        claimed_digest, claimed_stat = _fresh_digest(claimed, cancel, pause)
    except OSError:
        return False
    if ((victim_stat.st_dev, victim_stat.st_ino)
            == (claimed_stat.st_dev, claimed_stat.st_ino)):
        return False  # той самий inode — не незалежна копія
    return victim_digest == claimed_digest


def _publish_no_overwrite(
    source_name: str, source_dir_fd: int,
    dest_name: str, dest_dir_fd: int,
) -> bool:
    """Опублікувати source_name як dest_name БЕЗ перезапису наявної цілі.

    Повертає True при успіху; False якщо ціль уже існує (кличучий підбирає
    інше ім'я з суфіксом). Інші помилки прокидає нагору.

    Основний шлях — жорстке посилання + видалення джерела: os.link атомарно
    падає FileExistsError, якщо ціль існує, тож ніколи не перезаписує (APFS/
    HFS+). На ФС без жорстких посилань (exFAT/FAT/msdos/SMB) os.link дає
    ENOTSUP; тоді ім'я резервується ексклюзивно через O_EXCL і власне джерело
    перейменовується поверх ЦІЄЇ САМОЇ заглушки. rename перезаписує лише
    щойно створену нами порожню заглушку, тому інваріант «жодного перезапису
    чужих даних» тримається і без підтримки посилань.
    """
    try:
        os.link(
            source_name, dest_name,
            src_dir_fd=source_dir_fd, dst_dir_fd=dest_dir_fd,
            follow_symlinks=False,
        )
    except FileExistsError:
        return False
    except OSError as error:
        if error.errno not in _LINK_UNSUPPORTED:
            raise
        try:
            reserved = os.open(
                dest_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                dir_fd=dest_dir_fd,
            )
        except FileExistsError:
            return False
        os.close(reserved)
        try:
            os.rename(
                source_name, dest_name,
                src_dir_fd=source_dir_fd, dst_dir_fd=dest_dir_fd,
            )
        except OSError:
            # Ніколи не лишати порожню заглушку після невдалої публікації.
            try:
                os.unlink(dest_name, dir_fd=dest_dir_fd)
            except OSError:
                pass
            raise
        return True
    else:
        os.unlink(source_name, dir_fd=source_dir_fd)
        return True


def _transfer_files(
    res, plan, dst_dir: str, src_dir: str | None = None,
    expected_digests: dict[str, str] | None = None,
    root_identities: dict[str, tuple[int, int]] | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
    *,
    mode: str,
    skipped_out: list[tuple[str, str]] | None = None,
):
    """Спільне перевірене ядро move/copy для workflow подібних тек.

    mode="move": ексклюзивний hard link + unlink джерела (файл без
    file_meta переноситься — він не доказаний як дублікат).
    mode="copy": приватна тимчасова копія у цільовій теці, повторний
    BLAKE3, метадані, і лише тоді ексклюзивна публікація hard link-ом.
    Ціль НІКОЛИ не перезаписується; конкурентне ім'я дає суфікс-retry.

    Рядок плану, чий вміст УЖЕ лежить у цілі побайтово, не переноситься
    зовсім: пара (джерело, ціль) додається в `skipped_out` — обидва шляхи,
    бо викликач (наприклад workers._merge_similar_worker) будує з них
    dict(skipped) для extra_survivors. Анотація раніше
    казала `list[str]`, хоча код завжди клав туди tuple — контракт розійшовся
    з фактичною поведінкою, ruff і тести цього не ловили.
    """
    if mode not in ("move", "copy"):
        raise ValueError(f"невідомий режим переносу: {mode}")
    copying = mode == "copy"
    transferred: list[tuple[str, str]] = []
    errors: list[str] = []
    skipped = skipped_out if skipped_out is not None else []
    cancel = cancel or threading.Event()
    total_bytes = sum(size for size, _src, _rel in plan) * (2 if copying else 1)
    completed_bytes = 0
    last_progress = [0.0]
    last_phase = [""]

    def report(phase: str, done: int, total: int, *, force: bool = False) -> None:
        # Гейт НЕ обходиться зміною phase: phase містить шлях поточного файла,
        # тому раніше оновлення летіли на КОЖНОМУ файлі й смикали інтерфейс.
        now = time.monotonic()
        if progress is not None and (
            force or now - last_progress[0] >= 0.1 or done == total
        ):
            last_progress[0] = now
            last_phase[0] = phase
            progress(phase, done, total)

    normalized_roots = {
        os.path.abspath(path): identity
        for path, identity in (root_identities or {}).items()
    }
    expected_src_root = normalized_roots.get(os.path.abspath(src_dir)) if src_dir else None
    expected_dst_root = normalized_roots.get(os.path.abspath(dst_dir))
    for size, src, rel in plan:
        if not _operation_wait(cancel, pause):
            break
        parent_fd = -1
        source_parent_fd = -1
        temporary_name = ""
        try:
            fi = res.file_meta.get(src)
            bad_plan = (
                os.path.isabs(rel) or rel in ("", ".")
                or (src_dir is not None
                    and (not _contained(src_dir, src)
                         or rel != os.path.relpath(src, src_dir))))
            if copying:
                # fi is None у copy: без карти digest-ів план не доказаний і
                # відхиляється (сирі виклики _copy_files — суворі, як у
                # 2.16). Якщо ж карта є (шлях через MergePreparationWorker),
                # digest здобуто свіжим повним читанням — копіювання з
                # потоковою звіркою безпечне і для файлів поза сканом
                # (__pycache__ та інші EXCLUDE_NAMES). У move такий файл
                # ОБОВ'ЯЗКОВО переноситься в обох випадках.
                bad_plan = (fi is None and expected_digests is None) or bad_plan
            if bad_plan:
                word = "копіювання" if copying else "переносу"
                errors.append(f"{src}: небезпечний план {word}")
                continue
            source_root = src_dir or os.path.dirname(src)
            source_parent_fd, source_name = _safe_source_parent(
                source_root, src, expected_src_root)
            verb = "копіюванням" if copying else "переносом"

            def file_progress(read_bytes: int, _total: int) -> None:
                report(
                    f"Перевіряю перед {verb} · {src}",
                    completed_bytes + read_bytes,
                    total_bytes,
                )

            is_symlink = os.path.islink(src)
            if is_symlink:
                # Немає вмісту для BLAKE3: verify_current_file безумовно
                # йде за посиланням (open() слідує за symlink) і хешує
                # ЦІЛЬОВИЙ файл, а звіряється з lstat-розміром самого
                # лінка — гарантована невідповідність, ESTALE на КОЖНОМУ
                # symlink у плані злиття (E2E-хотфікс 3).
                digest, source_stat = core.verify_current_symlink(src)
            else:
                digest, source_stat = core.verify_current_file(
                    src, fi, cancel=cancel, pause=pause,
                    progress=file_progress)
            if not _operation_wait(cancel, pause):
                break
            expected = (
                expected_digests.get(src)
                if expected_digests is not None
                else _class_digest(res.file_class.get(src))
            )
            if expected_digests is not None and expected is None:
                errors.append(f"{src}: немає свіжого контрольного хешу")
                continue
            if expected is not None and digest != expected:
                errors.append(f"{src}: змінився після скану")
                continue
            desired = os.path.join(dst_dir, rel)
            if not _contained(dst_dir, desired):
                errors.append(f"{src}: шлях виходить за межі цільової теки")
                continue
            parent = os.path.dirname(desired)
            parent_fd = _safe_destination_parent(
                dst_dir, parent, expected_dst_root)
            # Ціль уже має цей самий вміст → переносити нічого. Перевірка
            # стоїть ДО створення тимчасової копії, щоб не читати й не
            # писати даремно цілий файл.
            if _destination_holds_same_content(
                    os.path.basename(desired), parent_fd, digest,
                    source_stat, is_symlink):
                # Разом зі шляхом жертви запам'ятовується ДОКАЗ — та копія,
                # збіг з якою щойно доведено повним читанням. Без неї крок
                # «теку в Кошик» не мав би чим виправдати неохоплений файл.
                skipped.append((src, desired))
                continue
            if copying and is_symlink:
                # Немає вмісту для тимчасового файла+fsync-дансу нижче —
                # відтворюємо як НОВИЙ symlink з тим самим текстом цілі,
                # свіжо звіреним, і публікуємо тим самим ексклюзивним
                # os.link(..., follow_symlinks=False)-ретраєм, що й
                # звичайні файли (E2E-хотфікс 3).
                temporary_name = (
                    f".dupscan-copy-{os.getpid()}-{threading.get_ident()}-"
                    f"{time.time_ns()}")
                try:
                    link_target = os.readlink(source_name, dir_fd=source_parent_fd)
                except OSError as error:
                    raise OSError(
                        f"посилання зникло перед копіюванням: {error}") from error
                fresh_digest = blake3.blake3(
                    link_target.encode("utf-8", "surrogatepass")).hexdigest()
                if fresh_digest != digest:
                    raise OSError("ціль посилання змінилася перед копіюванням")
                os.symlink(link_target, temporary_name, dir_fd=parent_fd)
                if not _operation_wait(cancel, pause):
                    break
            elif copying:
                temporary_name = (
                    f".dupscan-copy-{os.getpid()}-{threading.get_ident()}-"
                    f"{time.time_ns()}")
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_fd,
                )
                copied_hash = blake3.blake3(
                    max_threads=core._blake3_threads(size, True))
                source_descriptor = os.open(
                    source_name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=source_parent_fd,
                )
                copy_source_before = os.fstat(source_descriptor)
                if (
                    _trash_identity(copy_source_before) != _trash_identity(source_stat)
                    or not stat_mod.S_ISREG(copy_source_before.st_mode)
                ):
                    os.close(source_descriptor)
                    raise OSError("джерело змінилося перед копіюванням")
                with (
                    os.fdopen(descriptor, "wb") as output,
                    os.fdopen(source_descriptor, "rb", buffering=0) as source,
                ):
                    copied_bytes = 0
                    while True:
                        if not _operation_wait(cancel, pause):
                            raise OSError(
                                errno.ECANCELED,
                                "копіювання скасовано")
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        copied_hash.update(chunk)
                        copied_bytes += len(chunk)
                        report(
                            f"Копіюю · {src}",
                            completed_bytes + size + copied_bytes,
                            total_bytes,
                        )
                    if copied_bytes != size:
                        raise OSError("коротке читання під час копіювання")
                    if copied_hash.hexdigest() != digest:
                        raise OSError("джерело змінилося під час копіювання")
                    if _trash_identity(os.fstat(source.fileno())) != _trash_identity(
                        copy_source_before
                    ):
                        raise OSError("джерело змінилося під час копіювання")
                    os.fchmod(output.fileno(), stat_mod.S_IMODE(source_stat.st_mode))
                    output.flush()
                    os.fsync(output.fileno())
                    # Дати виставляються ПІСЛЯ публікації (_apply_source_dates):
                    # тут вони не втрималися б — flush/close дописують дані й
                    # знову підіймають mtime на «зараз».
                if not _operation_wait(cancel, pause):
                    break
            base_name, extension = os.path.splitext(os.path.basename(desired))
            destination_name = os.path.basename(desired)
            for number in range(1, _MAX_SUFFIX_ATTEMPTS + 1):
                if not _operation_wait(cancel, pause):
                    break
                if copying:
                    if _publish_no_overwrite(
                            temporary_name, parent_fd,
                            destination_name, parent_fd):
                        temporary_name = ""
                        destination = os.path.join(parent, destination_name)
                        _apply_source_dates(
                            destination_name, parent_fd, destination,
                            source_stat)
                        transferred.append((src, destination))
                        break
                    # Ім'я зайняте. Якщо там той самий вміст (гонитва або
                    # зайнятий суфікс) — це не привід плодити ще один
                    # дублікат: рядок пропускається.
                    if _destination_holds_same_content(
                            destination_name, parent_fd, digest,
                            source_stat, is_symlink):
                        skipped.append(
                            (src, os.path.join(parent, destination_name)))
                        break
                    destination_name = (
                        f"{base_name} ({number + 1}){extension}")
                else:
                    # The re-stat is deliberately immediately adjacent to
                    # link: verification must not authorize a replacement
                    # source inode.
                    current = os.stat(
                        source_name,
                        dir_fd=source_parent_fd,
                        follow_symlinks=False,
                    )
                    # symlink-запис лишається symlink-ом (тип не змінюється
                    # на REG чи навпаки) — E2E-хотфікс 3: цей же гейт
                    # раніше безумовно вимагав S_ISREG і валив ПЕРЕНОС
                    # будь-якого symlink-запису плану.
                    still_expected_type = (
                        stat_mod.S_ISLNK(current.st_mode) if is_symlink
                        else stat_mod.S_ISREG(current.st_mode))
                    if (_trash_identity(current) != _trash_identity(source_stat)
                            or not still_expected_type):
                        raise OSError("джерело змінилося перед переносом")
                    if _publish_no_overwrite(
                            source_name, source_parent_fd,
                            destination_name, parent_fd):
                        destination = os.path.join(parent, destination_name)
                        transferred.append((src, destination))
                        break
                    # Той самий вміст уже в цілі: файл лишається в джерелі,
                    # і крок «теку в Кошик» потім знайде для нього живу
                    # копію ПОЗА текою — саме її наявність і дозволяє Кошик.
                    if _destination_holds_same_content(
                            destination_name, parent_fd, digest,
                            current, is_symlink):
                        skipped.append(
                            (src, os.path.join(parent, destination_name)))
                        break
                    destination_name = (
                        f"{base_name} ({number + 1}){extension}")
            else:
                raise FileExistsError("не вдалося підібрати вільне ім’я")
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError) and not copying:
                raise  # move історично ловив лише OSError
            if not cancel.is_set():
                errors.append(f"{src}: {error}")
        finally:
            if temporary_name and parent_fd >= 0:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except OSError:
                    pass
            if parent_fd >= 0:
                os.close(parent_fd)
            if source_parent_fd >= 0:
                os.close(source_parent_fd)
        if cancel.is_set():
            break
        completed_bytes += size * (2 if copying else 1)
        done_word = "Скопійовано й перевірено" if copying else "Перенесено"
        report(
            f"{done_word} · {src}", completed_bytes, total_bytes, force=True)
    return transferred, errors


def _move_files(
    res, plan, dst_dir: str, src_dir: str | None = None,
    expected_digests: dict[str, str] | None = None,
    root_identities: dict[str, tuple[int, int]] | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
    skipped_out: list[tuple[str, str]] | None = None,
):
    """Verified no-overwrite move for the similar-folder workflow.

    A source root supplied by the UI binds every untrusted plan row to its
    declared relative path.  Publication is an exclusive hard link followed
    by unlinking the source, so a destination created concurrently is never
    overwritten.
    """
    return _transfer_files(
        res, plan, dst_dir, src_dir, expected_digests, root_identities,
        cancel, pause, progress, mode="move", skipped_out=skipped_out)


def _copy_files(
    res, plan, dst_dir: str, src_dir: str | None = None,
    expected_digests: dict[str, str] | None = None,
    root_identities: dict[str, tuple[int, int]] | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
    skipped_out: list[tuple[str, str]] | None = None,
):
    """Verified, no-overwrite copy used by the similar-folder workflow.

    Each source is re-read, copied into a private same-directory temporary,
    re-hashed, then published using an exclusive hard link. A concurrent file
    can therefore cause a suffix retry but can never be overwritten.
    """
    return _transfer_files(
        res, plan, dst_dir, src_dir, expected_digests, root_identities,
        cancel, pause, progress, mode="copy", skipped_out=skipped_out)


def _verify_then_trash_dir(
    res, src_dir: str,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
    trash=None,
    survivor=None,
    extra_survivors: dict[str, str] | None = None,
    unproven_out: list[str] | None = None,
):
    """Фоновий потік: пройти РЕАЛЬНИЙ диск теки і переконатися, що КОЖЕН
    лишковий файл має живу копію поза нею; лише тоді вся тека — у Кошик.

    Symlink-и не блокують операцію: вони входять у метаданий знімок теки як
    текст цілі, тому будь-яка їхня зміна валить операцію так само надійно.

    Файли без доказу дубліката (не охоплені профілем скану — `__pycache__`
    та інші EXCLUDE_NAMES) робили крок Кошика неможливим, тому злиття
    переносило їх у ціль заздалегідь. Відколи злиття вміє ПРОПУСКАТИ той
    самий вміст замість плодити ` (2)`-дублікат, такий файл лишається в
    джерелі — і має бути виправданий інакше: `extra_survivors` несе шлях
    копії, знайденої під час злиття, а `_survivor_by_content` перечитує з
    диска і жертву, і цю копію. Доказ з часу злиття на віру НЕ береться.

    Повертає ("abort", кількість_беззахисних) або ("ok", помилки_кошика)."""
    # Bracket survivor verification with full Merkle snapshots. A file added,
    # removed, replaced or edited while the verification is running must make
    # the whole-directory Trash step fail closed.
    trash = trash if trash is not None else to_trash
    survivor = survivor if survivor is not None else _verified_survivor
    cancel = cancel or threading.Event()
    mib = 1024 * 1024
    last_progress = [0.0]

    source_identity = _trash_identity(os.lstat(src_dir))
    if source_identity[2] != stat_mod.S_IFDIR:
        raise OSError(errno.ENOTDIR, "очікувалась звичайна тека", src_dir)

    def snapshot_progress(path: str, read_bytes: int, files_done: int) -> None:
        now = time.monotonic()
        if progress is not None and now - last_progress[0] >= 0.1:
            last_progress[0] = now
            progress(
                f"Знімок теки · {path} · МіБ",
                (read_bytes + mib - 1) // mib,
                0,
            )

    before = core.snapshot_directory_state(
        src_dir, cancel=cancel, pause=pause, progress=snapshot_progress)
    bad = 0
    removal = {src_dir}
    survivor_cache: dict = {}  # доказ копії — один раз на клас
    walk_errors: list[OSError] = []
    for dirpath, dns, fns in os.walk(
            src_dir, followlinks=False, onerror=walk_errors.append):
        if not _operation_wait(cancel, pause):
            return "cancelled", 0
        for name in fns:
            if not _operation_wait(cancel, pause):
                return "cancelled", 0
            if core.is_appledouble(name) and core._appledouble_sibling_exists(
                    dirpath, name):
                # Супутник AppleDouble не має і не може мати доказу дубліката:
                # це метадані сусіднього файла, а не самостійний вміст. Вимога
                # доказу до нього блокувала Кошик для цілком доказаної теки.
                # У Кошик він їде разом із текою, як і на APFS — де ці самі
                # метадані невидимо лежать в inode. Але це виправдання чинне
                # ЛИШЕ коли сусід X справді лежить поруч (байтове ім'я) — без
                # нього «._X» це файл із чужим доказом, а не супутник
                # падає у звичайний доказ
                # нижче, як будь-який інший файл.
                continue
            p = os.path.join(dirpath, name)
            try:
                if os.path.islink(p):
                    continue  # у знімку як ціль; вмісту не має
                if not survivor(
                        res, p, removal, cancel=cancel, pause=pause,
                        survivor_cache=survivor_cache):
                    # Немає класу в скані — але злиття могло довести копію
                    # прямим читанням. Перевіряємо це твердження заново.
                    if not _survivor_by_content(
                            p, (extra_survivors or {}).get(p), removal,
                            cancel=cancel, pause=pause):
                        bad += 1
                        # Форма повернення ("abort", кількість) стабільна для
                        # старих викликів; ХТО саме недоведений — через
                        # out-параметр, за прецедентом skipped_out
                        # (звіт замість голого числа).
                        if unproven_out is not None:
                            unproven_out.append(p)
            except OSError:
                bad += 1
                if unproven_out is not None:
                    unproven_out.append(p)
    bad += len(walk_errors)
    if cancel.is_set():
        return "cancelled", 0
    try:
        if core.snapshot_directory_state(
                src_dir, cancel=cancel, pause=pause,
                progress=snapshot_progress) != before:
            bad += 1
        if _trash_identity(os.lstat(src_dir)) != source_identity:
            bad += 1
    except OSError:
        bad += 1
    if bad:
        return "abort", bad
    if cancel.is_set():
        return "cancelled", 0
    return "ok", trash(
        [src_dir], expected_identities={src_dir: source_identity})


# ---- Автофікс імен (fskit-фантоми + NFD-профілактика) ----------------------
#
# Виміряно на диску власника: fskit перелічує NFD-імена (scandir бачить),
# але namei (stat/open) дає ENOENT навіть за точними байтами через dir_fd;
# /.vol і fsgetpath не підтримані fskit; Finder цих файлів не бачить
# узагалі. ЄДИНИЙ НЕвипробуваний раніше локальний syscall — rename за
# точним записом. На свіжому NFD-файлі він адресує запис і робить файл
# доступним; на фантомі — невідомо (може дати ENOENT так само). Тому
# fix_name_to_nfc ПРОБУЄ і чесно КЛАСИФІКУЄ результат — не обіцянка
# "гарантовано полагодити".
#
# ДРУГЕ вимірювання (на реальному hdiutil-змонтованому exFAT-образі, готуючи
# ЦЕЙ примітив): RENAME_EXCL там підтримується лише ЧАСТКОВО. Контрольовано,
# по 3 незалежні спроби на кожен бік:
#   ціль ВЖЕ існує   -> renamex_np(..., RENAME_EXCL) завжди дає EEXIST (3/3);
#   ціль ВІДСУТНЯ    -> той самий виклик завжди дає ENOTSUP (3/3) — драйвер
#                       exFAT/fskit не вміє атомарного "створити-якщо-
#                       відсутнє", хоча правильно РОЗПІЗНАЄ наявну ціль.
# APFS підтримує RENAME_EXCL повністю в обох випадках (жодного ENOTSUP).
# Наслідок: сліпе мапування ENOTSUP -> "error" робило б "fixed" НЕДОСЯЖНИМ
# на exFAT — саме тій ФС, заради якої існує вся фіча. Тому ENOTSUP/
# EOPNOTSUPP — ОКРЕМА гілка: драйвер ЩЕ ПЕРЕД цією відповіддю мусив би
# перевірити ціль (інакше не зміг би віддати EEXIST у сусідньому випадку),
# тож ENOTSUP тут — фактичне підтвердження відсутності цілі, і лише ТОДІ
# пробуємо звичайний rename (без EXCL) як єдиний працюючий шлях. Це
# лишає короткий, апаратно неусувний TOCTOU-проміжок (два системні виклики
# поспіль, без затримки між ними) — прийнятно для одноразової ручної дії
# одного користувача, і НІКОЛИ не зачіпає гілку "ціль існує" (та завжди
# ловиться першим, EXCL-викликом, до будь-якого фолбека).

_RENAME_EXCL = 0x00000004  # <sys/attr.h>: rename БЕЗ перезапису наявної цілі
# errno, що (емпірично) означають "цей MOUNT не підтримує RENAME_EXCL для
# 'створити-якщо-відсутнє'" — не "інша помилка": ENOTSUP спостережено на
# exFAT/fskit; EOPNOTSUPP лишено про запас (той самий сенс на інших
# драйверах/версіях macOS, різні числові значення на цій платформі).
_RENAME_EXCL_UNSUPPORTED_ERRNOS = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP})


def _renamex_np_libc():
    """Окремий кеш-хендл від _libc()/_statfs_libc() вище: свій набір
    argtypes на тому самому дескрипторі libc (той самий патерн)."""
    handle = getattr(_renamex_np_libc, "_handle", "unset")
    if handle == "unset":
        try:
            handle = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            handle.renamex_np.argtypes = [
                ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
            ]
            handle.renamex_np.restype = ctypes.c_int
        except (OSError, AttributeError, TypeError):
            handle = None
        # Той самий патерн кешу-на-функції, що _libc() вище.
        _renamex_np_libc._handle = handle  # type: ignore[attr-defined]
    return handle


def _renamex_np(old_path: str, new_path: str, flags: int) -> None:
    """renamex_np(2) з довільними прапорами (тонка обгортка, БЕЗ політики).

    НЕ os.rename(): звичайний POSIX rename(2) МОВЧКИ перезаписує наявну
    ціль. flags=RENAME_EXCL дає атомарну відмову (EEXIST) там, де ФС це
    підтримує — ctypes, той самий прийом, що _set_creation_time/
    _statfs_fstype вище в модулі. flags=0 — звичайний rename, той самий
    ефект, що дав би os.rename() (використовується лише як контрольований
    фолбек у fix_name_to_nfc нижче, коли EXCL сам підтвердив ENOTSUP —
    не замість, а ПІСЛЯ спроби безпечного шляху).

    Кидає OSError(errno, strerror, old_path) на будь-якому провалі —
    контракт, сумісний з os.rename(), щоб виклик коду розрізняв причину
    через errno так само, як для звичайного rename.
    """
    lib = _renamex_np_libc()
    if lib is None:
        raise OSError(errno.ENOSYS, "renamex_np недоступний на цій системі")
    ctypes.set_errno(0)
    rc = lib.renamex_np(os.fsencode(old_path), os.fsencode(new_path), flags)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), old_path)


def fix_name_to_nfc(dirpath: str, name: str) -> tuple[str, str]:
    """Спробувати перейменувати не-NFC (типово NFD) ім'я на канонічний NFC.

    Повертає (outcome, detail) — ЧЕСНИЙ вердикт по КОНКРЕТНОМУ файлу, не
    обіцянка "полагоджено":
    - "already-nfc" — ім'я вже канонічне; rename не пробувався, no-op;
    - "fixed" — перейменовано; detail = запитане (NFC) ім'я, яким файл
      ГАРАНТОВАНО відтепер адресується (lookup нормалізовано-нечутливий
      — виміряно і на APFS, і на цьому exFAT/fskit). НЕ обіцянка щодо
      точних байтів на диску: цей exFAT/fskit-драйвер сам перекодовує
      КОЖЕН запис у NFD, незалежно від того, що передано в rename() —
      виміряно готуючи tests/test_namefix.py (навіть пряме створення
      файла NFC-байтами лягає на диск як NFD). Той самий факт пояснює,
      чому "фікс" функціонально працює, попри це: після rename файл
      доступний і під NFC-, і під NFD-байтовою формою імені однаково;

    - "phantom" — rename дав ENOENT: fskit перелічує запис через
      scandir, але жоден syscall (stat/open/rename) не адресує його —
      файл цілий, але локально не полагодити; потрібен інший ПК/драйвер;
    - "exists" — ціль із NFC-іменем уже існує (EEXIST). Завжди ловиться
      ПЕРШИМ, атомарним RENAME_EXCL-викликом (не фолбеком нижче) — rename
      НЕ виконався, ОБИДВА файли лишаються на місці — нуль перезапису;
    - "protected" — dirpath/name під текою-еталоном
      (preferences.is_protected) — консервативно: еталон недоторканний і
      для перейменувань. Той самий вердикт, коли конфігурацію еталонів
      узагалі не вдалось прочитати — fail-closed, як і to_trash вище:
      не можемо підтвердити відсутність еталона, тож не перейменовуємо;
    - "error" — інша OSError (чи renamex_np недоступний на цій ОС);
      detail = текст помилки.

    Точні байти імені: *name* приймається як є (str зі scandir, може
    містити NFD-послідовності) — fsencode() усередині _renamex_np()
    кодує ЙОГО байти, не пере-нормалізовану підміну. ЛИШЕ rename — жодних
    видалень, копій чи інших побічних дій.

    RENAME_EXCL-фолбек (див. коментар над _RENAME_EXCL вище): якщо перша
    спроба дає ENOTSUP/EOPNOTSUPP (ФС не підтримує атомарне "створити-
    якщо-відсутнє" — виміряно на exFAT), а НЕ EEXIST, це вже означає, що
    цілі немає (інакше перша спроба дала б EEXIST), тож другий виклик
    (без EXCL) — єдиний працюючий шлях до "fixed" на такій ФС. Гілка
    "ціль існує" ніколи не доходить до фолбека.
    """
    nfc_name = unicodedata.normalize("NFC", name)
    if name == nfc_name:
        return "already-nfc", name
    old_path = os.path.join(dirpath, name)
    try:
        reference_roots = preferences.load_reference_roots()
    except preferences.PreferencesError as error:
        return "protected", f"конфігурація тек-еталонів не читається: {error}"
    if preferences.is_protected(old_path, reference_roots):
        return "protected", ""
    new_path = os.path.join(dirpath, nfc_name)
    try:
        _renamex_np(old_path, new_path, _RENAME_EXCL)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return "phantom", ""
        if error.errno == errno.EEXIST:
            return "exists", ""
        if error.errno not in _RENAME_EXCL_UNSUPPORTED_ERRNOS:
            return "error", str(error)
        try:
            _renamex_np(old_path, new_path, 0)
        except OSError as fallback_error:
            if fallback_error.errno == errno.ENOENT:
                return "phantom", ""
            if fallback_error.errno == errno.EEXIST:
                # Гонитва: щось з'явилось під NFC-іменем МІЖ двома
                # викликами вище (мікросекунди без затримки між ними) —
                # той самий чесний вердикт, що і в атомарному випадку.
                return "exists", ""
            return "error", str(fallback_error)
        return "fixed", nfc_name
    return "fixed", nfc_name
