"""QThread-воркери DupScan: скан, рескан сесії, перевірка пари, злиття,
Кошик теки, завантаження сесії, перерахунок, разова фонова робота.

Винесено з app.py (2.17 «Solid Core») БЕЗ зміни поведінки, разом із
чистими помічниками рескану сесії (containing_session_root,
rebase_session_path, _iter_session_refresh_cache_rows,
build_session_refresh_cache_rows, _session_refresh_stats, _dir_dates_for),
які не торкаються GUI. Диск — тільки тут, ніколи в GUI-потоці.
"""

from __future__ import annotations

import errno
import os
import stat as stat_mod
import sys
import threading
import time

from PySide6.QtCore import QThread, Signal

import dupscan.infra.cache as cache
import dupscan.domain.core as core
import dupscan.infra.devices as devices
import dupscan.ui.perceptual as perceptual
import dupscan.infra.preferences as preferences
import dupscan.infra.session as session
import dupscan.infra.throttle as throttle
from dupscan.infra.fsops import _copy_files, _directory_identity, _move_files


def _app_hooks():
    """Точки перехоплення операцій (app._same_device,
    app._verify_then_trash_dir із trash/survivor-хуками) живуть у
    просторі імен dupscan.ui.app і підмінюються тестами й інтеграціями;
    воркери читають їх пізно.

    У PyInstaller-збірці app.py виконується як __main__ — модуля
    "dupscan.ui.app" у sys.modules НЕ існує (KeyError ламав злиття і
    Кошик теки лише у зібраному застосунку, 2.17.0). Тому:
    dupscan.ui.app → __main__ (той самий файл під збірковим іменем) →
    fail-safe fsops (та сама логіка операцій, лише без можливості
    підміни)."""
    import dupscan.infra.fsops as fsops
    for name in ("dupscan.ui.app", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "_same_device") \
                and hasattr(module, "_verify_then_trash_dir"):
            return module
    return fsops


def containing_session_root(path: str, roots: tuple[str, ...] | list[str]) -> str | None:
    """Return the longest historical root containing *path*, without I/O."""
    normalized = os.path.normpath(os.path.abspath(path))
    matches: list[str] = []
    for raw_root in roots:
        root = os.path.normpath(os.path.abspath(raw_root))
        try:
            if os.path.commonpath((root, normalized)) == root:
                matches.append(root)
        except ValueError:
            continue
    return max(matches, key=len, default=None)


def rebase_session_path(path: str, old_root: str, new_root: str) -> str:
    """Translate one historical absolute path under an explicitly chosen root."""
    path = os.path.normpath(os.path.abspath(path))
    old_root = os.path.normpath(os.path.abspath(old_root))
    new_root = os.path.normpath(os.path.abspath(new_root))
    try:
        if os.path.commonpath((old_root, path)) != old_root:
            raise ValueError
    except ValueError as error:
        raise ValueError(f"шлях не належить кореню сесії: {path}") from error
    relative = os.path.relpath(path, old_root)
    translated = new_root if relative == "." else os.path.join(new_root, relative)
    try:
        if os.path.commonpath((new_root, translated)) != new_root:
            raise ValueError
    except ValueError as error:
        raise ValueError(f"переприв’язаний шлях виходить за новий корінь: {path}") from error
    return os.path.normpath(translated)


def _iter_session_refresh_cache_rows(
        previous: core.ScanResult,
        session_roots: tuple[str, ...] | list[str],
        root_map: dict[str, str] | None = None):
    """Yield trustworthy historical full hashes at their resolved paths.

    This performs no filesystem I/O. ``core.scan`` obtains fresh lstat
    metadata and ``HashCache.get_many`` accepts a row only when the complete
    identity still matches.
    """
    roots = tuple(dict.fromkeys(
        os.path.normpath(os.path.abspath(root)) for root in session_roots
    ))
    mappings = {
        os.path.normpath(os.path.abspath(old)):
        os.path.normpath(os.path.abspath(new))
        for old, new in (root_map or {}).items()
    }
    seen: set[str] = set()
    for raw_path, info in previous.file_meta.items():
        old_path = os.path.normpath(os.path.abspath(raw_path))
        old_root = containing_session_root(old_path, roots)
        if old_root is None:
            continue
        file_class = previous.file_class.get(raw_path, "")
        size_text, separator, digest = file_class.partition(":")
        if (
            separator != ":"
            or size_text != str(info.size)
            or len(digest) != 64
            or digest != digest.lower()
            or any(char not in "0123456789abcdef" for char in digest)
            # Legacy sessions lacking full identity are deliberately cold.
            or info.ctime_ns <= 0
            or info.dev <= 0
            or info.ino <= 0
        ):
            continue
        new_root = mappings.get(old_root, old_root)
        path = rebase_session_path(old_path, old_root, new_root)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        yield (
            path, "f", info.size, info.mtime_ns, info.ctime_ns,
            info.dev, info.ino, digest,
        )


def build_session_refresh_cache_rows(
        previous: core.ScanResult,
        session_roots: tuple[str, ...] | list[str],
        root_map: dict[str, str] | None = None) -> list[tuple]:
    """Return deterministic cache seed rows; intended for audit/tests."""
    return sorted(
        _iter_session_refresh_cache_rows(previous, session_roots, root_map),
        key=lambda row: os.path.normcase(row[0]),
    )


def _session_refresh_stats(
        previous: core.ScanResult, current: core.ScanResult,
        session_roots: tuple[str, ...], root_map: dict[str, str]) -> dict[str, int]:
    """Compare snapshots in a worker; no filesystem access."""
    old: dict[str, core.FileInfo] = {}
    for path, info in previous.file_meta.items():
        root = containing_session_root(path, session_roots)
        if root is None:
            continue
        translated = rebase_session_path(
            path, root, root_map.get(root, root))
        old[os.path.normcase(translated)] = info
    new = {os.path.normcase(path): info for path, info in current.file_meta.items()}
    shared = old.keys() & new.keys()

    def identity(info: core.FileInfo) -> tuple[int, int, int, int, int]:
        return (info.size, info.mtime_ns, info.ctime_ns, info.dev, info.ino)

    return {
        "added": len(new.keys() - old.keys()),
        "removed": len(old.keys() - new.keys()),
        "changed": sum(identity(old[path]) != identity(new[path])
                       for path in shared),
    }


def _dir_dates_for(
    result,
    cancel: threading.Event | None = None,
) -> dict[str, tuple[int, int]]:
    """Дати (створено, змінено) тек із груп. lstat по диску — кликати ЛИШЕ
    поза GUI-потоком (скан-воркер, фонове завантаження сесії)."""
    out: dict[str, tuple[int, int]] = {}
    for g in result.dir_groups:
        for p in g.paths:
            if cancel is not None and cancel.is_set():
                raise OSError(
                    errno.ECANCELED, "підготовку дат сесії скасовано", p)
            try:
                st = os.lstat(p)
                bt = getattr(st, "st_birthtime_ns", None) or int(
                    getattr(st, "st_birthtime", st.st_mtime) * 1e9)
                out[p] = (bt, st.st_mtime_ns)
            except OSError:
                pass
    return out



class ScanWorker(QThread):
    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, roots: list[str], cache: cache.HashCache | None = None,
                 profile: preferences.ScanProfile | None = None):
        super().__init__()
        self.roots = roots
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.cache = cache
        self.profile = profile
        self.dir_dates: dict[str, tuple[int, int]] = {}
        # None means save was not attempted yet; "" is an explicit failure.
        self.saved_session_path: str | None = None

    def run(self):
        owned_cache = self.cache is None
        # governor живе рівно на час скану, на
        # власному daemon-потоці — окрема подія (не self.cancel: той сигналить
        # лише РУЧНЕ скасування користувачем, ніколи не встановлюється при
        # звичайному завершенні, тож governor_stop інакше повис би назавжди
        # після кожного успішного скану).
        activity = throttle.DiskActivity()
        adaptive = throttle.AdaptiveConcurrency(
            cap=devices.adaptive_cap(self.roots), activity=activity)
        governor_stop = threading.Event()
        governor_thread = threading.Thread(
            target=adaptive.run, args=(governor_stop,), daemon=True)
        governor_thread.start()
        try:
            if owned_cache:
                self.cache = cache.HashCache.open(
                    progress=self.progress.emit,
                    cancel=self.cancel,
                    pause=self.pause,
                )
            r = core.scan(self.roots, progress=self.progress.emit,
                          cancel=self.cancel, cache=self.cache, pause=self.pause,
                          profile=self.profile, adaptive=adaptive, activity=activity)
            # важка пост-обробка — ТУТ, у скан-потоці, доки результат ще не
            # відданий GUI (нуль гонок і нуль замерзань: on_done лише показує):
            self.dir_dates = _dir_dates_for(r, self.cancel)
            self.saved_session_path = session.save_session(
                r, r.scanned_roots or self.roots, partial=r.partial)
            self.done.emit(r)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        finally:
            governor_stop.set()
            governor_thread.join(timeout=1.0)
            if owned_cache and self.cache is not None:
                self.cache.close()
                self.cache = None


class PerceptualScanWorker(QThread):
    """«Схожі фото (підказка)» — фоновий
    воркер, диск лише тут, ніколи в GUI-потоці. Послідовний (не пул) —
    perceptual.find_similar_images сам такий, підказка не мусить
    ускладнюватись паралелізмом заради опційної, некритичної для доказу
    смуги."""

    progress = Signal(str, int, int)
    done = Signal(object)  # perceptual.PerceptualResult
    failed = Signal(str)

    def __init__(self, roots: list[str]):
        super().__init__()
        self.roots = roots
        self.cancel = threading.Event()
        self.pause = threading.Event()

    def run(self):
        try:
            result = perceptual.find_similar_images(
                self.roots, progress=self.progress.emit,
                cancel=self.cancel, pause=self.pause)
            self.done.emit(result)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class SessionRefreshWorker(QThread):
    """Authoritative refresh of one historical snapshot.

    Old state is read-only. Cache seeding, traversal, hashing, aggregation,
    directory dates and saving the new live session all stay off the GUI
    thread.
    """

    progress = Signal(str, int, int)
    done = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
            self, roots: list[str], previous: core.ScanResult, *,
            session_roots: tuple[str, ...] | list[str] = (),
            root_map: dict[str, str] | None = None,
            source_session_path: str | None = None):
        super().__init__()
        self.roots = list(dict.fromkeys(
            os.path.normpath(os.path.abspath(root)) for root in roots
        ))
        self.previous = previous
        self.session_roots = tuple(dict.fromkeys(
            os.path.normpath(os.path.abspath(root)) for root in session_roots
        ))
        self.root_map = {
            os.path.normpath(os.path.abspath(old)):
            os.path.normpath(os.path.abspath(new))
            for old, new in (root_map or {}).items()
        }
        self.source_session_path = (
            os.path.normpath(os.path.abspath(source_session_path))
            if source_session_path else None
        )
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.dir_dates: dict[str, tuple[int, int]] = {}
        self.stats = {"added": 0, "removed": 0, "changed": 0}
        # Number of exact historical BLAKE3 rows offered to the identity-
        # validating cache. Changed identities become misses during scan.
        self.reused_hashes = 0
        self.saved_session_path = ""

    def _wait(self) -> bool:
        while self.pause.is_set() and not self.cancel.wait(0.05):
            pass
        return not self.cancel.is_set()

    def _root_identities(
            self,
            expected: dict[str, tuple[int, int]] | None = None,
            ) -> dict[str, tuple[int, int]]:
        identities: dict[str, tuple[int, int]] = {}
        for root in self.roots:
            try:
                root_stat = os.lstat(root)
            except OSError as error:
                raise ValueError(
                    f"Поточний корінь недоступний: {root}: {error}"
                ) from error
            if not stat_mod.S_ISDIR(root_stat.st_mode):
                raise ValueError(
                    "Поточний корінь більше не є реальною текою "
                    f"(symlink також заборонено): {root}")
            identity = (root_stat.st_dev, root_stat.st_ino)
            if expected is not None and expected.get(root) != identity:
                raise ValueError(
                    "Поточний корінь було замінено після перевірки; "
                    f"сканування скасовано: {root}")
            identities[root] = identity
        return identities

    def run(self):
        local_cache = None
        try:
            if not self._wait():
                self.cancelled.emit()
                return
            root_identities = self._root_identities()
            local_cache = cache.HashCache.open(
                progress=self.progress.emit,
                cancel=self.cancel,
                pause=self.pause,
            )
            batch: list[tuple] = []
            for row in _iter_session_refresh_cache_rows(
                    self.previous, self.session_roots, self.root_map):
                if not self._wait():
                    self.cancelled.emit()
                    return
                batch.append(row)
                if len(batch) >= 500:
                    local_cache.put_many(batch)
                    self.reused_hashes += len(batch)
                    self.progress.emit(
                        "Підготовка перевірених BLAKE3",
                        self.reused_hashes, 0)
                    batch = []
            if batch:
                local_cache.put_many(batch)
                self.reused_hashes += len(batch)
                self.progress.emit(
                    "Підготовка перевірених BLAKE3",
                    self.reused_hashes, self.reused_hashes)
            if not self._wait():
                self.cancelled.emit()
                return
            self._root_identities(root_identities)
            result = core.scan(
                self.roots, progress=self.progress.emit, cancel=self.cancel,
                cache=local_cache, pause=self.pause, profile=None)
            if self.cancel.is_set() or result.partial:
                self.cancelled.emit()
                return
            # A remount/root replacement during a long scan must never be
            # published as one coherent current session.
            self._root_identities(root_identities)
            self.dir_dates = _dir_dates_for(result, self.cancel)
            self.stats = _session_refresh_stats(
                self.previous, result, self.session_roots, self.root_map)
            if not self._wait():
                self.cancelled.emit()
                return
            self._root_identities(root_identities)
            # Пряме передавання замість **dict-розпаковки —
            # save_kwargs["preserve_paths"] = (...) робив дводе-**kwargs з
            # неоднорідними типами параметрів save_session (base_dir: str|None
            # проти preserve_paths: tuple), mypy консервативно вважав, що
            # tuple міг би піти й у base_dir. Поведінка та сама: порожній
            # tuple — це власний дефолт save_session.
            preserve_paths = (
                (self.source_session_path,)
                if self.source_session_path is not None else ()
            )
            saved_path = session.save_session(
                result, result.scanned_roots or self.roots,
                partial=False, preserve_paths=preserve_paths)
            if not saved_path:
                raise RuntimeError(
                    "Актуальні дані перевірено, але нову сесію не вдалося "
                    "зберегти. Історичний знімок лишився без змін.")
            self.saved_session_path = saved_path
            # Successful atomic save is the publication boundary. A cancel
            # arriving after it must not claim that the durable session was
            # discarded.
            self.done.emit(result)
        except Exception as error:  # noqa: BLE001 — safe worker boundary
            if self.cancel.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(error))
        finally:
            if local_cache is not None:
                local_cache.close()


class PairVerificationWorker(QThread):
    """Fresh, cancellable scan of exactly two folders before session merge.

    Historical sessions remain read-only. This worker creates a separate,
    in-memory proof for just the requested A/B pair and never saves or
    replaces the loaded session.
    """

    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, dir_a: str, dir_b: str):
        super().__init__()
        self.roots = [os.path.abspath(dir_a), os.path.abspath(dir_b)]
        self.cancel = threading.Event()
        self.pause = threading.Event()

    def run(self):
        local_cache = None
        try:
            # Reject aliases and overlapping trees before core.scan can read
            # either target. This is both a safety boundary and a guarantee
            # that a rejected selective check performs no broad parent scan.
            core.validate_merge_roots(self.roots[0], self.roots[1])
            local_cache = cache.HashCache.open(
                progress=self.progress.emit,
                cancel=self.cancel,
                pause=self.pause,
            )
            # Deliberately no user profile: destructive merge verification may
            # not skip hidden files, bundles, symlinks, or small content.
            result = core.scan(
                self.roots, progress=self.progress.emit, cancel=self.cancel,
                cache=local_cache, pause=self.pause, profile=None)
            self.done.emit(result)
        except Exception as error:  # noqa: BLE001 — report safely to GUI
            self.failed.emit(str(error))
        finally:
            if local_cache is not None:
                local_cache.close()


class MergePreparationWorker(QThread):
    """Cancellable read-only merge plan plus fresh per-source BLAKE3 proof."""

    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, result: core.ScanResult, src_dir: str, dst_dir: str):
        super().__init__()
        self.result = result
        self.src_dir = src_dir
        self.dst_dir = dst_dir
        self.cancel = threading.Event()
        self.pause = threading.Event()

    def run(self):
        try:
            plan, total = core.merge_plan(
                self.result, self.src_dir, self.dst_dir)
            root_identities = {
                os.path.abspath(self.src_dir): _directory_identity(self.src_dir),
                os.path.abspath(self.dst_dir): _directory_identity(self.dst_dir),
            }
            # Навіть повільний/завислий stat зовнішнього тому має лишатися у
            # worker thread. GUI отримує результат разом із готовим планом.
            same_device = _app_hooks()._same_device(
                self.src_dir, self.dst_dir)
            expected_digests: dict[str, str] = {}
            mib = 1024 * 1024
            total_units = max(1, (total + mib - 1) // mib) if plan else 0
            completed_bytes = 0
            last_progress = 0.0
            for size, source, _relative in plan:
                if self.cancel.is_set():
                    self.done.emit(None)
                    return
                info = self.result.file_meta.get(source)
                # info is None — файл поза охопленням скану: службові теки
                # EXCLUDE_NAMES (__pycache__, .git…) або профільні фільтри.
                # merge_plan свідомо кладе такі файли в план (uncovered —
                # «усе без доказу мусить переїхати»), а доказом для них є
                # свіже повне читання нижче (verify_current_file без
                # очікуваних метаданих). Раніше тут був вибух «немає
                # метаданих файла», що ламав злиття будь-якої теки зі
                # службовим сміттям.

                def file_progress(read_bytes: int, _file_total: int) -> None:
                    nonlocal last_progress
                    now = time.monotonic()
                    if now - last_progress < 0.1:
                        return
                    last_progress = now
                    done_units = min(
                        total_units,
                        (completed_bytes + read_bytes + mib - 1) // mib,
                    )
                    self.progress.emit(
                        f"Перевіряю перед злиттям · {source} · МіБ",
                        done_units,
                        total_units,
                    )

                if os.path.islink(source):
                    # symlink: немає вмісту для BLAKE3 (verify_current_file
                    # безумовно йде за посиланням і хешує ЦІЛЬОВИЙ файл —
                    # E2E-хотфікс 3, core.verify_current_symlink).
                    digest, _current = core.verify_current_symlink(source)
                else:
                    digest, _current = core.verify_current_file(
                        source, info, cancel=self.cancel, pause=self.pause,
                        progress=file_progress)
                expected_digests[source] = digest
                completed_bytes += size
                now = time.monotonic()
                if now - last_progress >= 0.1 or completed_bytes >= total:
                    last_progress = now
                    self.progress.emit(
                        f"Перевірено перед злиттям · {source} · МіБ",
                        min(total_units, (completed_bytes + mib - 1) // mib),
                        total_units,
                    )
            if self.cancel.is_set():
                self.done.emit(None)
                return
            for root, expected in root_identities.items():
                if _directory_identity(root) != expected:
                    raise OSError(
                        errno.ESTALE,
                        "кореневу теку замінено під час підготовки злиття",
                        root,
                    )
            self.done.emit((
                plan,
                total,
                expected_digests,
                same_device,
                root_identities,
            ))
        except OSError as error:
            if self.cancel.is_set():
                self.done.emit(None)
            else:
                self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 — safely report worker failure
            self.failed.emit(str(error))


class MergeTransferWorker(QThread):
    """Pause/cancel-aware copy or atomic-per-file move."""

    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)
    mutates_files = True

    def __init__(
        self, mode: str, result: core.ScanResult, plan: list[tuple],
        dst_dir: str, src_dir: str,
        expected_digests: dict[str, str],
        root_identities: dict[str, tuple[int, int]] | None = None,
    ):
        super().__init__()
        if mode not in ("copy", "move"):
            raise ValueError("невідомий режим злиття")
        self.mode = mode
        self.result = result
        self.plan = plan
        self.dst_dir = dst_dir
        self.src_dir = src_dir
        self.expected_digests = expected_digests
        self.root_identities = dict(root_identities or {})
        self.cancel = threading.Event()
        self.pause = threading.Event()

    def run(self):
        try:
            helper = _copy_files if self.mode == "copy" else _move_files
            skipped: list[tuple[str, str]] = []
            completed, errors = helper(
                self.result,
                self.plan,
                self.dst_dir,
                self.src_dir,
                self.expected_digests,
                self.root_identities,
                cancel=self.cancel,
                pause=self.pause,
                progress=self.progress.emit,
                skipped_out=skipped,
            )
            self.done.emit(
                (completed, errors, self.cancel.is_set(), skipped))
        except Exception as error:  # noqa: BLE001 — worker boundary
            if self.cancel.is_set():
                self.done.emit(([], [], True, []))
            else:
                self.failed.emit(str(error))


class DirectoryTrashWorker(QThread):
    """Cancellable proof before the one final system-Trash call."""

    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)
    mutates_files = True

    def __init__(self, result: core.ScanResult, source_dir: str,
                 extra_survivors: dict[str, str] | None = None):
        super().__init__()
        self.result = result
        self.source_dir = source_dir
        # Копії, доведені під час злиття для файлів без класу в скані.
        # Гейт перечитує їх з диска сам — це підказка ДЕ шукати, не доказ.
        self.extra_survivors = dict(extra_survivors or {})
        # ЯКІ саме файли лишились без доказу при abort: голе число нічого
        # не каже власнику. Заповнює гейт.
        self.unproven: list[str] = []
        self.cancel = threading.Event()
        self.pause = threading.Event()

    def run(self):
        try:
            value = _app_hooks()._verify_then_trash_dir(
                self.result,
                self.source_dir,
                cancel=self.cancel,
                pause=self.pause,
                progress=self.progress.emit,
                extra_survivors=self.extra_survivors,
                unproven_out=self.unproven,
            )
            self.done.emit(value)
        except Exception as error:  # noqa: BLE001 — worker boundary
            if self.cancel.is_set():
                self.done.emit(("cancelled", 0))
            else:
                self.failed.emit(str(error))


class SessionLoadWorker(QThread):
    """Streaming, cancellable historical-session load.

    Parsing, validation, aggregation and optional directory-date probes all
    stay outside the GUI thread.  A cancelled load never publishes a partial
    in-memory result.
    """

    progress = Signal(str, int, int)
    done = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, path: str, *, load_directory_dates: bool):
        super().__init__()
        self.path = path
        self.load_directory_dates = load_directory_dates
        self.cancel = threading.Event()

    def run(self):
        try:
            result = session.load_session(
                self.path,
                cancel=self.cancel,
                progress=self.progress.emit,
            )
            dates = (
                _dir_dates_for(result, self.cancel)
                if self.load_directory_dates
                else {}
            )
            if self.cancel.is_set():
                self.cancelled.emit()
                return
            self.done.emit((result, dates))
        except OSError as error:
            if self.cancel.is_set() or error.errno == errno.ECANCELED:
                self.cancelled.emit()
            else:
                self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 — safe worker boundary
            if self.cancel.is_set():
                self.cancelled.emit()
            else:
                self.failed.emit(str(error))


class RecomputeWorker(QThread):
    """Перерахунок після видалень — у фоні, щоб вікно ніколи не замерзало
    (на 60k-файловому дереві перерахунок займає секунди)."""

    done = Signal()
    failed = Signal(str)

    def __init__(self, result, removed: set[str]):
        super().__init__()
        self.result = result
        self.removed = removed
        self.cancel = threading.Event()

    def run(self):
        try:
            if core.recompute(
                self.result, self.removed, cancel=self.cancel
            ):
                self.done.emit()
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class Bg(QThread):
    """Одноразова фонова робота: ('ok', результат) або ('err', текст) —
    сигналом назад у GUI-потік. GUI-потік НІКОЛИ не торкається диска сам."""

    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(("ok", self._fn()))
        except Exception as e:  # noqa: BLE001 — фонова помилка не валить GUI
            self.done.emit(("err", str(e)))
