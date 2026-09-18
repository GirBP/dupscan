"""Постійний кеш хешів DupScan: SQLite-таблиця path+kind -> digest.

best-effort за дизайном: жодна помилка (пошкоджена БД, немає прав на теку,
диск readonly) не має валити скан. HashCache.open() НІКОЛИ не кидає — при
будь-якій проблемі повертає інстанс у "вимкненому" режимі (get завжди None,
put_many/flush/close — no-op). Кеш НЕ знає нічого про воронку чи скасування —
це відповідальність виклику в core.scan(): писати сюди лише справжні digest,
ніколи None з перерваного/помилкового читання.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time

_DB_NAME = "hashes.db"
_MAX_ROWS = 1_000_000
_DELETE_BATCH = 250_000
_STALE_DAYS = 180
_TOUCH_DAYS = 7
_VACUUM_MIN_BYTES = 256 * 1024 * 1024
_VACUUM_RESERVE_BYTES = 256 * 1024 * 1024


def _wait_for_control(
    cancel: threading.Event | None, pause: threading.Event | None
) -> bool:
    while (
        pause is not None
        and pause.is_set()
        and not (cancel is not None and cancel.is_set())
    ):
        time.sleep(0.05)
    return not (cancel is not None and cancel.is_set())


def _report(progress, phase: str, done: int, total: int) -> None:
    if progress is None:
        return
    try:
        progress(phase, done, total)
    except Exception:  # noqa: BLE001 — UI progress cannot disable correctness cache
        pass


def _default_base_dir() -> str:
    return os.environ.get("DUPSCAN_DATA_DIR") or os.path.expanduser(
        "~/Library/Application Support/DupScan"
    )


class HashCache:
    """path+kind -> digest з повною ідентичністю файла зі stat()."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @classmethod
    def open(
        cls, base_dir: str | None = None, progress=None,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
    ) -> "HashCache":
        self = cls()
        conn: sqlite3.Connection | None = None
        try:
            base = base_dir if base_dir is not None else _default_base_dir()
            os.makedirs(base, exist_ok=True)
            db_path = os.path.join(base, _DB_NAME)
            conn = sqlite3.connect(
                db_path, check_same_thread=False, timeout=3.0
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS hashes ("
                "path TEXT NOT NULL, kind TEXT NOT NULL, "
                "size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, "
                "ctime_ns INTEGER NOT NULL DEFAULT 0, "
                "dev INTEGER NOT NULL DEFAULT 0, ino INTEGER NOT NULL DEFAULT 0, "
                "accessed_ns INTEGER NOT NULL DEFAULT 0, digest TEXT NOT NULL, "
                "PRIMARY KEY(path, kind))"
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(hashes)")}
            for name in ("ctime_ns", "dev", "ino", "accessed_ns"):
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE hashes ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                    )
            check = conn.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError("hash cache failed quick_check")
            cls._maintain(
                conn, db_path, progress=progress,
                cancel=cancel, pause=pause)
            self._conn = conn
        except Exception:  # noqa: BLE001 — best-effort: будь-яка проблема -> вимкнений режим
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._conn = None
        return self

    @staticmethod
    def _delete_batched(
        conn: sqlite3.Connection, condition: str, parameters: tuple = (),
        progress=None,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
    ) -> int:
        """Keep WAL bounded while deleting millions of legacy rows."""
        removed = 0
        while True:
            if not _wait_for_control(cancel, pause):
                break
            cursor = conn.execute(
                "DELETE FROM hashes WHERE rowid IN ("
                f"SELECT rowid FROM hashes WHERE {condition} LIMIT ?"
                ")",
                (*parameters, _DELETE_BATCH),
            )
            count = max(0, cursor.rowcount)
            conn.commit()
            removed += count
            if count:
                _report(progress, "Очищення кешу", removed, 0)
            if count < _DELETE_BATCH:
                break
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return removed

    @classmethod
    def _maintain(
        cls, conn: sqlite3.Connection, db_path: str, progress=None,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
    ) -> None:
        """Bound cache growth without ever affecting scan correctness."""
        cutoff = (
            time.time_ns()
            - _STALE_DAYS * 24 * 60 * 60 * 1_000_000_000
        )
        # Rows from pre-identity cache versions cannot satisfy strict
        # size+mtime+ctime+dev+ino validation and only consume disk.
        cls._delete_batched(
            conn,
            "(accessed_ns=0 OR ctime_ns=0 OR dev=0 OR ino=0 "
            "OR accessed_ns < ?)",
            (cutoff,),
            progress=progress,
            cancel=cancel,
            pause=pause,
        )
        if not _wait_for_control(cancel, pause):
            return
        row_count = int(conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0])
        overflow = max(0, row_count - _MAX_ROWS)
        while overflow:
            if not _wait_for_control(cancel, pause):
                return
            amount = min(overflow, _DELETE_BATCH)
            conn.execute(
                "DELETE FROM hashes WHERE rowid IN ("
                "SELECT rowid FROM hashes ORDER BY accessed_ns ASC LIMIT ?)",
                (amount,),
            )
            conn.commit()
            overflow -= amount
            _report(
                progress, "Обмеження кешу",
                row_count - overflow, row_count)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        try:
            db_bytes = os.path.getsize(db_path)
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
            free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            live_bytes = max(page_size, (pages - free_pages) * page_size)
            free_disk = shutil.disk_usage(os.path.dirname(db_path)).free
            enough_room = (
                free_disk >= live_bytes * 2 + _VACUUM_RESERVE_BYTES
            )
            worthwhile = (
                db_bytes >= _VACUUM_MIN_BYTES
                and free_pages >= max(1, pages // 4)
            )
            if (
                worthwhile and enough_room
                and _wait_for_control(cancel, pause)
            ):
                _report(progress, "Ущільнення кешу", 0, 0)
                conn.execute("VACUUM")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except OSError:
            # Lack of space information only skips compaction; logical cache
            # maintenance already committed and scan correctness is unchanged.
            pass

    def _disable(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def get(self, path: str, size: int, mtime_ns: int, kind: str) -> str | None:
        if self._conn is None:
            return None
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT size, mtime_ns, accessed_ns, digest "
                    "FROM hashes WHERE path=? AND kind=?",
                    (path, kind),
                ).fetchone()
        except Exception:  # noqa: BLE001
            self._disable()
            return None
        if row is None:
            return None
        db_size, db_mtime_ns, accessed_ns, digest = row
        if db_size == size and db_mtime_ns == mtime_ns:
            self._touch([(path, kind, accessed_ns)])
            return digest
        return None

    def _touch(
        self,
        rows: list[tuple[str, str, int]],
        *,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
        progress=None,
    ) -> None:
        if self._conn is None or not rows:
            return
        now = time.time_ns()
        threshold = now - _TOUCH_DAYS * 24 * 60 * 60 * 1_000_000_000
        stale = [(now, path, kind) for path, kind, accessed in rows
                 if accessed < threshold]
        if not stale:
            return
        try:
            with self._lock:
                if self._conn is None:
                    return
                try:
                    self._conn.execute("BEGIN")
                    for index in range(0, len(stale), 500):
                        if not _wait_for_control(cancel, pause):
                            self._conn.rollback()
                            return
                        self._conn.executemany(
                            "UPDATE hashes SET accessed_ns=? "
                            "WHERE path=? AND kind=?",
                            stale[index : index + 500],
                        )
                        _report(
                            progress, "Оновлення кешу",
                            min(index + 500, len(stale)), len(stale))
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
        except Exception:  # noqa: BLE001
            self._disable()

    def get_many(
        self,
        kind: str,
        items: list[tuple],
        *,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
        progress=None,
    ) -> dict[str, str]:
        """items = [(path,size,mtime,ctime,dev,ino), ...] одного kind.

        Трійки старого API підтримуються лише для сумісності тестів; сканер
        завжди передає повну ідентичність.
        для гарячого шляху hash_stage: один SELECT на ~500 шляхів замість
        одного запиту на файл (на 60k+ файлах саме роздрібні запити, а не
        хешування, були вузьким місцем теплого re-scan — див. бенч)."""
        out: dict[str, str] = {}
        if self._conn is None or not items:
            return out
        by_path = {}
        for item in items:
            if len(item) == 3:
                path, size, mtime_ns = item
                by_path[path] = (size, mtime_ns, None, None, None)
            else:
                path, size, mtime_ns, ctime_ns, dev, ino = item
                by_path[path] = (size, mtime_ns, ctime_ns, dev, ino)
        paths = list(by_path)
        step = 500
        touched: list[tuple[str, str, int]] = []
        try:
            with self._lock:
                for i in range(0, len(paths), step):
                    if not _wait_for_control(cancel, pause):
                        break
                    batch = paths[i : i + step]
                    qmarks = ",".join("?" * len(batch))
                    rows = self._conn.execute(
                        "SELECT path, size, mtime_ns, ctime_ns, dev, ino, "
                        "accessed_ns, digest "
                        "FROM hashes "
                        f"WHERE kind=? AND path IN ({qmarks})",
                        (kind, *batch),
                    ).fetchall()
                    for (path, db_size, db_mtime_ns, db_ctime_ns, db_dev,
                         db_ino, accessed_ns, digest) in rows:
                        want_size, want_mtime_ns, want_ctime, want_dev, want_ino = by_path[path]
                        legacy = want_ctime is None
                        if (db_size == want_size and db_mtime_ns == want_mtime_ns
                                and (legacy or (db_ctime_ns == want_ctime
                                                and db_dev == want_dev
                                                and db_ino == want_ino))):
                            out[path] = digest
                            touched.append((path, kind, accessed_ns))
                    _report(
                        progress, "Перевірка кешу",
                        min(i + step, len(paths)), len(paths))
                # Keep this write outside the SELECT loop but under the same
                # cache operation. _touch takes the lock itself.
        except Exception:  # noqa: BLE001
            self._disable()
            return out
        self._touch(
            touched, cancel=cancel, pause=pause, progress=progress)
        return out

    def put_many(
        self,
        rows: list[tuple],
        *,
        cancel: threading.Event | None = None,
        pause: threading.Event | None = None,
        progress=None,
    ) -> None:
        """rows = [(path,kind,size,mtime,ctime,dev,ino,digest), ...].

        Старі п'ятірки приймаються для сумісності. Ніколи не передавай
        рядки з digest=None — виклик відповідає за те, щоб сюди йшли лише
        справжні, повністю обчислені хеші."""
        if self._conn is None or not rows:
            return
        now = time.time_ns()
        normalized = []
        for row in rows:
            if len(row) == 5:
                path, kind, size, mtime_ns, digest = row
                normalized.append((path, kind, size, mtime_ns, 0, 0, 0, now, digest))
            else:
                path, kind, size, mtime_ns, ctime_ns, dev, ino, digest = row
                normalized.append((path, kind, size, mtime_ns, ctime_ns,
                                   dev, ino, now, digest))
        try:
            with self._lock:
                try:
                    self._conn.execute("BEGIN")
                    for index in range(0, len(normalized), 500):
                        if not _wait_for_control(cancel, pause):
                            self._conn.rollback()
                            return
                        self._conn.executemany(
                            "INSERT OR REPLACE INTO hashes("
                            "path,kind,size,mtime_ns,ctime_ns,dev,ino,"
                            "accessed_ns,digest) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            normalized[index : index + 500],
                        )
                        _report(
                            progress, "Запис кешу",
                            min(index + 500, len(normalized)),
                            len(normalized),
                        )
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
        except Exception:  # noqa: BLE001
            self._disable()

    def flush(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.commit()
        except Exception:  # noqa: BLE001
            self._disable()

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            with self._lock:
                self._conn.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._disable()

    def __enter__(self) -> "HashCache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
