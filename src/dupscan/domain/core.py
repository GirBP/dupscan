"""DupScan core: дублікати файлів, дублікати папок, подібність папок.

Скан = обхід (авто: зовнішні томи /Volumes/* — BFS-хвилі на WALK_THREADS
потоках, worker-и лише читають теки, злиття стану — строго в одному потоці;
внутрішній диск — послідовний os.walk, він там швидший і лишається еталоном
тест-паритету) -> воронка (розмір -> початок -> повний BLAKE3; проміжні
стадії лише ВІДСІЮЮТЬ) -> агрегації. Агрегації винесено в _aggregate(), тому
recompute() перераховує всі три вкладки після видалень МИТТЄВО (без диска).
Ядро нічого не видаляє; безпека видалення — в UI (Кошик + перевірки).
"""

from __future__ import annotations

import os
import errno
import fcntl
import stat as stat_mod
import struct
import subprocess
import threading
import time
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from heapq import heappush, heapreplace
from typing import TYPE_CHECKING

import blake3

if TYPE_CHECKING:
    from dupscan.infra.cache import HashCache
    import dupscan.infra.throttle as throttle  # лише для типів — виклики нижче duck-typed
    from dupscan.infra.preferences import ScanProfile

PARTIAL = 128 * 1024
CHUNK = 1024 * 1024
BIG = 16 * 1024 * 1024  # поріг для багатопотокового BLAKE3 (full-режим)
BIG_CHUNK = 8 * 1024 * 1024  # більші чанки на великих файлах — усе одно з cancel-гейтом
WORKERS = min(8, max(2, os.cpu_count() or 4))
WALK_THREADS = 8  # обхід обмежений диском, не CPU; 8 ховає латентність тек
FANOUT_CAP = 64
MAX_PAIRS = 400
MAX_SHARED_ROWS = 60  # рядків «спільні файли» на пару подібності
MAX_ERROR_DETAILS = 10_000
EXCLUDE_NAMES = {".git", "node_modules", ".Trash", "__pycache__",
                 # службові теки зовнішніх томів macOS — шум і помилки прав:
                 ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
                 ".DocumentRevisions-V100"}
EXCLUDE_PREFIXES = ("/System", "/usr", "/bin", "/sbin", "/Library", "/private/var/db")
BUNDLE_SUFFIXES = (".app", ".framework", ".bundle", ".photoslibrary", ".kext",
                   ".plugin", ".xcodeproj", ".pkg")


# slots: сотні тисяч екземплярів; __dict__ на кожен — це ~100 Б зайвих
@dataclass(slots=True)
class FileInfo:
    path: str
    size: int
    mtime_ns: int
    btime_ns: int  # дата створення (macOS st_birthtime)
    ctime_ns: int = 0
    dev: int = 0
    ino: int = 0


@dataclass
class FileGroup:
    size: int
    digest: str
    paths: list[str]
    # Кількість НЕЗАЛЕЖНИХ фізичних сховищ у групі: APFS-клони ділять
    # екстенти, тому їх видалення не звільняє місця. 0 = невідомо ->
    # консервативно вважаємо всі копії окремими (класична оцінка).
    families: int = 0
    # Чесний максимум звільнення: Σ реально зайнятого сім'ями сховища мінус
    # найменша сім'я. Розріджені/стиснуті файли рахуються за st_blocks,
    # не за логічним розміром. -1 = не обчислено -> класична формула.
    freeable: int = -1

    @property
    def wasted(self) -> int:
        if self.freeable >= 0:
            return self.freeable
        effective = self.families if self.families else len(self.paths)
        return self.size * (max(1, effective) - 1)


@dataclass
class DirGroup:
    size: int
    n_files: int
    paths: list[str]
    # Сім'ї сховища тек: клонована тека (cp -cR) ділить УСІ екстенти з
    # оригіналом — її видалення не звільняє місця. 0 = невідомо.
    families: int = 0

    @property
    def wasted(self) -> int:
        effective = self.families if self.families else len(self.paths)
        return self.size * (max(1, effective) - 1)


@dataclass
class SimPair:
    dir_a: str
    dir_b: str
    percent: float
    shared_bytes: int
    shared: list[tuple[int, str, str]] = field(default_factory=list)  # (size, файл в A, файл в B)
    shared_total: int = 0  # повна кількість спільних рядків (shared — кап показу)


@dataclass
class ScanResult:
    file_groups: list[FileGroup] = field(default_factory=list)
    dir_groups: list[DirGroup] = field(default_factory=list)
    sim_pairs: list[SimPair] = field(default_factory=list)
    files_seen: int = 0
    bytes_seen: int = 0
    errors: list[str] = field(default_factory=list)
    errors_total: int = 0
    partial: bool = False  # скасовано до завершення — групи можуть бути неповними
    # Історичні/імпортовані JSON-сесії примусово read-only. Лише результат
    # щойно виконаного scan() може бути джерелом руйнівної операції.
    live: bool = True
    # внутрішній стан для recompute() і перевірок перед видаленням:
    file_meta: dict[str, FileInfo] = field(default_factory=dict)
    file_class: dict[str, str] = field(default_factory=dict)
    class_size: dict[str, int] = field(default_factory=dict)
    class_paths: dict[str, list[str]] = field(default_factory=dict)
    dir_files: dict[str, list[str]] = field(default_factory=dict)
    dir_children: dict[str, list[str]] = field(default_factory=dict)
    # symlink-и не розіменовуються, але входять у Merkle-маніфест як
    # (повне ім'я лінка, текст target), тому папка з додатковим symlink не
    # може бути помилково оголошена дублікатом.
    dir_links: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # Додаткові directory entries тієї самої hardlink-сім'ї. Вони входять у
    # маніфест, але не в список звільнюваних файлових копій.
    dir_aliases: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    dir_ok: dict[str, bool] = field(default_factory=dict)
    # Підмножина тек з dir_ok[d] is False, де причина — СПРАВЖНІЙ збій
    # читання (permission/OSError/дивна відповідь диска), а не навмисне
    # виключення (EXCLUDE_NAMES, профіль). directory_tree_complete не
    # розрізняє ці два випадки і тому непридатна як гейт для вибіркового
    # злиття пари — для цього є directory_tree_mergeable нижче.
    dir_read_failed: set[str] = field(default_factory=set)
    # приховані користувачем пари подібності (канонічний порядок a<b);
    # поважаються агрегацією і переживають save/load сесії
    ignored_pairs: set[tuple[str, str]] = field(default_factory=set)
    # iCloud-файли без локального вмісту, пропущені щоб НІКОЛИ не викачувати
    # хмарний вміст без відома користувача
    dataless_skipped: int = 0
    # некритичні попередження (мережеві томи тощо)
    advisories: list[str] = field(default_factory=list)
    # канонізовані корені цього скану (_normalize_roots: realpath + дедуп);
    # сесія мусить зберігати САМЕ їх — сирі шляхи користувача можуть бути
    # симлінк-псевдонімами, які не проходять containment-валідацію при load
    scanned_roots: list[str] = field(default_factory=list)
    # фізичне сховище дубльованих файлів: path -> (dev, offset 1-го екстента);
    # з 2.15 персиститься в сесію — історична сесія чесна так само, як live
    file_storage: dict[str, tuple[int, int]] = field(default_factory=dict)
    # реально зайняте місце дубльованих файлів: path -> st_blocks * 512
    # (розріджені і стиснуті займають менше за логічний розмір)
    file_alloc: dict[str, int] = field(default_factory=dict)


def _record_error(res: ScanResult, message: object) -> None:
    res.errors_total += 1
    if len(res.errors) < MAX_ERROR_DETAILS:
        res.errors.append(str(message))


def _excluded(path: str, name: str) -> bool:
    normalized = os.path.normpath(os.path.abspath(path))
    return name in EXCLUDE_NAMES or any(
        normalized == prefix or normalized.startswith(prefix + os.sep)
        for prefix in EXCLUDE_PREFIXES
    )


def is_appledouble(name: str) -> bool:
    """Файл-супутник AppleDouble («._X») — метадані файла X, не файл користувача.

    На ФС без нативних xattr — exFAT/FAT/SMB, тобто рівно зовнішні диски, заради
    яких цей продукт і існує — macOS матеріалізує розширені атрибути й
    ресурс-форк файла X окремим файлом «._X» (магія 0x00051607, 4 КіБ).
    З macOS 14 система вішає `com.apple.provenance` на КОЖЕН створений файл,
    тож на такому томі супутник має практично кожен файл і кожна тека.
    Ядро зчіплює пару: rename(X) перейменовує «._X», unlink(X) видаляє його.

    Що ламалося, поки супутник був видимий (усе відтворено на змонтованому
    exFAT-образі, tests/test_fs_matrix*.py):
      * супутники однакового розміру й часто побайтово однакові — сканер
        збирав з них величезну ФАЛЬШИВУ групу дублікатів, а «звільнити місце»
        означало б знищити метадані живих файлів;
      * merge_plan брав у план і X, і «._X» — до рядка «._X» черга доходила
        вже після переносу X, який ядро супроводило переносом супутника, тож
        рядок падав [Errno 2] і давав власнику «N файл(ів) не перенесено»;
      * гейт Кошика вимагав доказу дубліката для супутника, якого той не має
        за визначенням, і відмовлявся відправляти доказану теку в Кошик.

    Тому супутник саме НЕВИДИМИЙ, а не «виключений»: виключення (EXCLUDE_NAMES)
    робить теку недоказовою і закриває гейт злиття, тоді як доводити тут нічого
    — на APFS ці самі дані просто не видно, вони лежать в inode. Так exFAT
    починає поводитись як APFS, а не як окремий світ.

    Правило за іменем, як у rsync/tar/git: користувацький файл, названий
    «._щось», буде помилково пропущений — але лише не потрапить у дедуп і
    ніколи не буде видалений, тобто помилка завжди у безпечний бік.
    """
    return name.startswith("._")


def _appledouble_sibling_exists(dirpath: str, name: str) -> bool:
    """Чи має «._X» (name) байтового сусіда X у ТОМУ Ж каталозі.

    Живе в core, і fsops кличе САМЕ її (власну копію звідти прибрано):
    core не може імпортувати fsops (fsops імпортує core — цикл), а обидва
    боки мають бачити ОДНЕ й те саме, інакше маніфест доказу й маніфест
    гейту розійдуться.
    Порівняння НЕ нормалізує Unicode (unicodedata.normalize не
    викликається на жодному боці): список імен каталогу звіряється
    рядок-у-рядок, як є. os.path.exists тут не годиться — пошук шляху на
    цій ФС сам нормалізує, тож NFD-сирота без байтового X пройшла б
    непомітно. Немає сусіда -> запис не
    вважається супутником ніде — ні в гейті, ні у знімках
    (те саме правило, що й нижче для симетричного випадку).
    """
    sibling = name[2:]
    if not sibling:
        return False
    try:
        with os.scandir(dirpath) as it:
            return any(entry.name == sibling for entry in it)
    except OSError:
        return False


def _is_bundle(name: str) -> bool:
    return name.lower().endswith(BUNDLE_SUFFIXES)


def _is_external(path: str) -> bool:
    """Чи лежить шлях на зовнішньому/знімному томі (/Volumes/…).

    /Volumes/<ім'я завантажувального тома> — симлінк на "/", це внутрішній
    диск. Паралельний обхід вмикається лише для зовнішніх: на внутрішньому
    SSD він ПОВІЛЬНІШИЙ за послідовний (заміряно), бо там нема латентності,
    яку варто ховати потоками."""
    p = os.path.abspath(path)
    if not p.startswith("/Volumes/"):
        return False
    vol = os.sep.join(p.split(os.sep)[:3])  # "/Volumes/<Назва>"
    return not (os.path.islink(vol) and os.path.realpath(vol) == "/")


FULL_PROOF = "full"  # єдиний тип доказу, з яким дозволена руйнівна дія
# Параноїдальний режим: повне перечитування навіть за чинного доказу.
# Керується налаштуванням користувача; за замовчуванням вимкнений, бо група
# взагалі не може виникнути без повного BLAKE3 (див. proof_kind).
PARANOID_VERIFICATION = False


def _same_identity(a: os.stat_result, b: os.stat_result) -> bool:
    """Та сама фізична сутність і той самий стан вмісту."""
    return ((a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns, a.st_ctime_ns)
            == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns, b.st_ctime_ns))


def proof_is_current(expected: "FileInfo", st: os.stat_result) -> bool:
    """Чи доказ (хеш зі скану) досі описує ЦЕЙ файл на диску.

    ЄДИНЕ джерело правди для порівняння метаданих; раніше ця логіка була
    продубльована в трьох місцях. Старі сесії мають нулі в dev/ino — для них
    порівнюємо доступну пару size+mtime.
    """
    if expected is None:
        return False
    if expected.dev:
        return ((st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns,
                 st.st_ctime_ns)
                == (expected.dev, expected.ino, expected.size,
                    expected.mtime_ns, expected.ctime_ns))
    return (st.st_size == expected.size
            and st.st_mtime_ns == expected.mtime_ns)


def proof_kind(res: "ScanResult", path: str) -> str | None:
    """Тип доказу для файла: "full" або None.

    Групи дублікатів формуються ВИКЛЮЧНО з повних BLAKE3 (див. воронку в
    scan()), тому належність до несинтетичного класу і є повним доказом.
    Тестовий/відновлювальний хук res.proof_kind_override дозволяє позначити
    доказ слабшим — тоді дія вимагатиме підвищення повним читанням.
    """
    override = getattr(res, "proof_kind_override", None)
    if override and path in override:
        return override[path]
    class_id = res.file_class.get(path)
    if not class_id or class_id.startswith("u:") or ":" not in class_id:
        return None
    return FULL_PROOF


SF_DATALESS = 0x40000000  # тіло файла живе в хмарі (iCloud), локально його нема
F_LOG2PHYS_EXT = 65  # fcntl: фізичне розташування логічного зміщення файла
_NETWORK_FS = {"smbfs", "nfs", "afpfs", "webdav", "cifs", "ftp"}


def _is_dataless(st) -> bool:
    """Файл без локального вмісту (iCloud). Читання = викачування з хмари."""
    return bool(getattr(st, "st_flags", 0) & SF_DATALESS)


def _storage_id(path: str) -> tuple[int, int] | None:
    """Ідентичність ФІЗИЧНОГО сховища: (dev, offset першого екстента).

    APFS-клони ділять екстенти — їхні id збігаються; справжні копії мають
    власні блоки (валідовано: 3/3 пари клонів виявлено, копії не позначені;
    st_blocks клони НЕ розрізняє). Використовується ЛИШЕ щоб не завищувати
    «звільнить»; БУДЬ-ЯКА помилка -> None -> класична консервативна оцінка."""
    fd = None
    try:
        st = os.lstat(path)
        if not stat_mod.S_ISREG(st.st_mode) or st.st_size <= 0:
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        # log2phys ядро трактує ПАКОВАНО (20 байт): flags(4) + contig(8) +
        # devoffset(8), БЕЗ вирівнювання. Читання з 24-байтним вирівняним
        # лейаутом давало offset>>32 і хибні колізії сусідніх алокацій
        # (впіймано власним тестом на 128КБ–4МБ файлах).
        request = struct.pack("<Iqq", 0, min(st.st_size, 1 << 20), 0)
        reply = fcntl.fcntl(fd, F_LOG2PHYS_EXT, request)
        _flags, _contig, offset = struct.unpack("<Iqq", reply)
        if offset <= 0:
            return None
        return (st.st_dev, offset)
    except (OSError, ValueError, struct.error):
        return None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _volume_fs_type(path: str) -> str:
    """Тип ФС тома, на якому лежить path (apfs/smbfs/…); "" якщо невідомо.

    Парсинг /sbin/mount за найдовшим mountpoint-префіксом — без ctypes і
    без залежності від приватних структур statfs."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    target = os.path.realpath(path)
    best_len, best_type = -1, ""
    for line in out.splitlines():
        try:
            _dev, rest = line.split(" on ", 1)
            mnt, opts = rest.rsplit(" (", 1)
            fstype = opts.split(",", 1)[0].rstrip(")")
        except ValueError:
            continue
        if ((target == mnt or target.startswith(mnt.rstrip("/") + "/"))
                and len(mnt) > best_len):
            best_len, best_type = len(mnt), fstype
    return best_type


def _normalize_roots(roots: list[str], errors: list[str]) -> list[str]:
    """Resolve symlink aliases, deduplicate and remove nested roots."""
    candidates: list[str] = []
    seen: set = set()
    for raw in roots:
        root = os.path.realpath(os.path.abspath(raw))
        if not os.path.isdir(root):
            errors.append(f"не тека: {raw}")
            continue
        # Дедуп за (dev, ino): на case-insensitive APFS «Dir» і «dir» — та
        # сама тека, а normcase на macOS — no-op (давало хибні дублікати
        # кожного файла при коренях у різному регістрі).
        # Key навмисно двох форм — справжня (dev, ino)-
        # ідентичність, або (коли навіть stat недоступний) слабший
        # запасний варіант за нормалізованим шляхом. Кортеж і рядок ніколи
        # не рівні одне одному, тож змішування в одному "seen" не ризикує
        # хибним дедупом між двома формами.
        key: tuple[int, int] | str
        try:
            st = os.stat(root)
            key = (st.st_dev, st.st_ino)
        except OSError:
            key = os.path.normcase(root)
        if key not in seen:
            seen.add(key)
            candidates.append(root)
    candidates.sort(key=lambda p: (p.count(os.sep), p.casefold()))
    kept: list[str] = []
    for root in candidates:
        if any(os.path.commonpath((parent, root)) == parent for parent in kept):
            continue
        kept.append(root)
    return kept


def _wait_if_paused(pause: threading.Event | None, cancel: threading.Event) -> None:
    """Блокує потік, поки встановлена пауза; cancel будить негайно."""
    if pause is None:
        return
    while pause.is_set() and not cancel.is_set():
        time.sleep(0.05)


def _hash_file(
    path: str, size: int, full: bool, cancel: threading.Event,
    pause: threading.Event | None = None, expected: FileInfo | None = None,
    progress=None, max_threads: int = 1,
) -> str | None:
    multi = full and size > BIG
    h = blake3.blake3(max_threads=max(1, max_threads))
    with open(path, "rb", buffering=0) as fh:
        before = os.fstat(fh.fileno())
        if not stat_mod.S_ISREG(before.st_mode) or before.st_size != size:
            raise OSError(errno.ESTALE, "файл змінився перед читанням", path)
        if expected is not None and not proof_is_current(expected, before):
            raise OSError(errno.ESTALE, "файл змінився після скану", path)
        if not full:
            want = min(PARTIAL, size)
            data = fh.read(want)
            if len(data) != want:
                raise OSError(errno.EIO, "коротке читання файла", path)
            h.update(data)
        else:
            chunk_size = BIG_CHUNK if multi else CHUNK
            remaining = size
            if progress is not None:
                progress(0, size)
            while remaining > 0:
                _wait_if_paused(pause, cancel)
                if cancel.is_set():
                    return None
                data = fh.read(min(chunk_size, remaining))
                if not data:
                    raise OSError(errno.EIO, "коротке читання файла", path)
                h.update(data)
                remaining -= len(data)
                if progress is not None:
                    progress(size - remaining, size)
        after = os.fstat(fh.fileno())
        if not _same_identity(before, after):
            raise OSError(errno.ESTALE, "файл змінився під час читання", path)
    return h.hexdigest()


def _blake3_threads(size: int, full: bool, parallel_files: int = 1) -> int:
    """Fair CPU budget for nested file-level and BLAKE3 parallelism."""
    if not full or size <= BIG:
        return 1
    cpus = max(1, os.cpu_count() or 1)
    return max(1, min(WORKERS, cpus // max(1, parallel_files)))


def _hash_file_parallelism(candidates: list[FileInfo], full: bool) -> int:
    """Choose file-level concurrency without turning HDD/NAS reads random.

    Partial probes stay highly parallel. Full reads from one external device
    are limited to two simultaneous files; separate volumes can each make
    progress. Internal/mixed workloads retain the CPU budget.
    """
    if not candidates:
        return 1
    cap = WORKERS
    if full and all(_is_external(info.path) for info in candidates):
        devices = {info.dev for info in candidates if info.dev}
        cap = min(WORKERS, max(2, len(devices) * 2))
    return min(cap, len(candidates))


def verify_current_file(
    path: str, expected: FileInfo | None = None,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
) -> tuple[str, os.stat_result]:
    """Повний актуальний BLAKE3 через відкритий fd + fstat до/після.

    Використовується безпосередньо перед руйнівними операціями. Повертає
    digest і фінальний stat; будь-яка зміна/коротке читання є OSError.
    """
    cancel = cancel or threading.Event()
    if cancel.is_set():
        raise OSError(errno.ECANCELED, "перевірку скасовано", path)
    _wait_if_paused(pause, cancel)
    if cancel.is_set():
        raise OSError(errno.ECANCELED, "перевірку скасовано", path)
    st = os.lstat(path)
    size = expected.size if expected is not None else st.st_size
    digest = _hash_file(
        path, size, True, cancel, pause=pause, expected=expected,
        progress=progress, max_threads=_blake3_threads(size, True))
    if digest is None:
        raise OSError(errno.ECANCELED, "перевірку скасовано", path)
    return digest, os.lstat(path)


def verify_current_symlink(path: str) -> tuple[str, os.stat_result]:
    """Свіжий доказ для symlink-запису плану злиття (E2E-хотфікс 3).

    verify_current_file читає ВМІСТ через open(path) — для symlink-шляху
    це БЕЗУМОВНО йде за посиланням і хешує ЦІЛЬОВИЙ файл, а розмір, з яким
    звіряється прочитане, лишається lstat-розміром самого лінка (довжина
    тексту цілі). Невідповідність гарантовано і ЩОРАЗУ валила
    MergePreparationWorker/_transfer_files з ESTALE на БУДЬ-ЯКОМУ дереві
    із symlink — snapshot_directory (Merkle-доказ перед Кошиком теки) вже
    так само окремо гілкує на stat_mod.S_ISLNK і хешує ТЕКСТ цілі (тег
    "L"), а не вміст; ця функція дає той самий, лише повторно застосовний,
    доказ для окремого symlink-запису плану переносу. Будь-яка зміна цілі
    між підготовкою і переносом дає інший digest — той самий fail-closed
    контракт, що й для звичайних файлів.
    """
    st = os.lstat(path)
    if not stat_mod.S_ISLNK(st.st_mode):
        raise OSError(errno.ESTALE, "більше не символічне посилання", path)
    try:
        target = os.readlink(path)
    except OSError as error:
        raise OSError(
            errno.ESTALE, "посилання зникло перед читанням", path) from error
    digest = blake3.blake3(
        target.encode("utf-8", "surrogatepass")).hexdigest()
    return digest, st


def _walk_stage(
    roots: list[str], res: ScanResult, profile: "ScanProfile | None",
    walk_threads: int, cancel: threading.Event,
    pause: threading.Event | None, tick,
) -> tuple[list[FileInfo], bool]:
    """Обхід дерева коренів: наповнює res.dir_ok/dir_files/dir_children/
    dir_links/dir_aliases/file_meta і повертає (знайдені файли, чи довершено
    без скасування). Послідовний os.walk чи паралельні BFS-хвилі —
    walk_threads>1 вмикає паралельний."""
    files: list[FileInfo] = []
    seen_paths: set[str] = set()
    seen_inodes: dict[tuple[int, int], str] = {}
    dir_files = res.dir_files
    dir_children = res.dir_children
    dir_links = res.dir_links
    dir_aliases = res.dir_aliases
    dir_ok = res.dir_ok

    def excluded_by_profile(path: str, name: str, *, directory: bool = False,
                            size: int = 0, symlink: bool = False) -> bool:
        """Apply an optional user profile without touching the filesystem.

        Directories do not participate in size/extension filters. A skipped
        entry poisons the enclosing exact-folder manifest, preventing a folder
        with deliberately unscanned content from being called an exact copy.
        """
        if profile is None:
            return False
        if directory:
            if name.startswith(".") and not profile.include_hidden:
                return True
            if _is_bundle(name) and not profile.include_bundles:
                return True
            try:
                normalized = os.path.normpath(path)
                if any(os.path.commonpath((normalized, parent)) == parent
                       for parent in profile.excluded_paths):
                    return True
            except ValueError:
                return True
            return False
        from dupscan.infra.preferences import should_include
        return not should_include(
            path, name=name, size=size, is_hidden=name.startswith("."),
            is_bundle=_is_bundle(name), profile=profile, is_symlink=symlink,
        )

    def take_file(dirpath: str, fp: str, st: os.stat_result) -> None:
        """Спільний фільтр файла для обох обходів: тип/
        повторний шлях/hardlink-сім'я. Кличеться ЛИШЕ зі скан-потоку."""
        if not stat_mod.S_ISREG(st.st_mode):
            return
        if _is_dataless(st):
            # тіло файла в хмарі: читання = тихе викачування гігабайтів.
            # Пропускаємо і робимо теку недоказовою (fail-closed).
            res.dataless_skipped += 1
            _record_error(res, f"iCloud-файл без локального вмісту — "
                               f"пропущено, щоб не викачувати з хмари: {fp}")
            dir_ok[dirpath] = False
            res.dir_read_failed.add(dirpath)
            return
        if excluded_by_profile(fp, os.path.basename(fp), size=st.st_size):
            # Навмисне виключення профілем, не збій — dir_read_failed НЕ
            # чіпаємо, щоб гейт пари не плутав його зі справжнім збоєм.
            dir_ok[dirpath] = False
            return
        key = os.path.normcase(fp)
        if key in seen_paths:
            return
        seen_paths.add(key)
        if st.st_nlink > 1:
            ik = (st.st_dev, st.st_ino)
            canonical = seen_inodes.get(ik)
            if canonical is not None:
                dir_aliases.setdefault(dirpath, []).append((fp, canonical))
                return
            seen_inodes[ik] = fp
        bt = getattr(st, "st_birthtime_ns", None) or int(
            getattr(st, "st_birthtime", st.st_mtime) * 1e9)
        fi = FileInfo(fp, st.st_size, st.st_mtime_ns, bt, st.st_ctime_ns,
                      st.st_dev, st.st_ino)
        files.append(fi)
        dir_files[dirpath].append(fp)
        res.file_meta[fp] = fi
        if len(files) % 512 == 0:
            tick("Обхід тек", len(files), 0)

    def take_link(dirpath: str, fp: str) -> None:
        # Default scans retain link metadata in exact-folder manifests. A
        # profile that omits links makes that manifest incomplete by design.
        if (profile is not None and excluded_by_profile(
                fp, os.path.basename(fp), symlink=True)):
            # Навмисне виключення профілем — не збій.
            dir_ok[dirpath] = False
            return
        try:
            target = os.readlink(fp)
        except OSError as e:
            _record_error(res, e)
            dir_ok[dirpath] = False
            res.dir_read_failed.add(dirpath)
            return
        dir_links.setdefault(dirpath, []).append((fp, target))

    def walk_sequential() -> bool:
        """Еталон: os.walk, тека за текою. False = скасовано."""
        visited: set[str] = set()
        for root in roots:
            root = os.path.abspath(root)
            if not os.path.isdir(root):
                _record_error(res, f"не тека: {root}")
                continue
            for dirpath, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=False,
                onerror=lambda error: _record_error(res, error),
            ):
                _wait_if_paused(pause, cancel)
                if cancel.is_set():
                    return False
                visit_key = os.path.normcase(os.path.realpath(dirpath))
                if visit_key in visited:
                    dirnames[:] = []
                    continue
                visited.add(visit_key)
                dir_ok.setdefault(dirpath, True)
                dir_files.setdefault(dirpath, [])
                dir_links.setdefault(dirpath, [])
                dir_aliases.setdefault(dirpath, [])
                keep = []
                for d in dirnames:
                    full_d = os.path.join(dirpath, d)
                    if (_excluded(full_d, d)
                            or excluded_by_profile(full_d, d, directory=True)):
                        # Непрочитана частина дерева робить маніфест неповним,
                        # але це НАВМИСНЕ виключення — не збій.
                        dir_ok[dirpath] = False
                        continue
                    if os.path.islink(full_d):
                        take_link(dirpath, full_d)
                        continue
                    keep.append(d)
                    dir_children.setdefault(dirpath, []).append(full_d)
                dirnames[:] = keep
                for name in filenames:
                    if is_appledouble(name):
                        continue  # метадані сусіднього файла, не файл
                    fp = os.path.join(dirpath, name)
                    try:
                        st = os.lstat(fp)
                    except OSError as e:
                        _record_error(res, e)
                        dir_ok[dirpath] = False
                        res.dir_read_failed.add(dirpath)
                        continue
                    if stat_mod.S_ISREG(st.st_mode):
                        take_file(dirpath, fp, st)
                    elif stat_mod.S_ISLNK(st.st_mode):
                        take_link(dirpath, fp)
                    else:
                        # Непідтримуваний тип запису (socket/FIFO/пристрій):
                        # не можемо чесно включити його в маніфест — це
                        # справжня деградація читання, не виключення.
                        dir_ok[dirpath] = False
                        res.dir_read_failed.add(dirpath)
        return True

    def scan_one_dir(dirpath: str):
        """Worker пулу: читає ОДНУ теку — самі лише scandir/lstat, жодного
        спільного стану. None = скасовано."""
        _wait_if_paused(pause, cancel)
        if cancel.is_set():
            return None
        subdirs: list[str] = []
        file_stats: list[tuple[str, os.stat_result]] = []
        links: list[tuple[str, str]] = []
        errs: list[str] = []
        ok = True
        # fskit-фантом (macOS exFAT-драйвер): нове ядро fskit інколи
        # ПЕРЕЛІЧУЄ запис через scandir, але стат на ньому падає ENOENT —
        # емпірично на кириличних NFD-іменах (fsck том OK, дані цілі).
        # Свіжий os.listdir(dirpath) — єдиний спосіб відрізнити це від
        # СПРАВЖНЬОГО зникнення файла між scandir і stat; рахуємо ЛІНИВО
        # (лише на перший ENOENT у цій теці) і кешуємо тут, щоб не робити
        # listdir на кожен запис.
        fresh_names: list[str] | None = None
        try:
            with os.scandir(dirpath) as it:
                for e in it:
                    try:
                        # Тека — перевіряється ПЕРШОЮ, до is_appledouble:
                        # супутник AppleDouble завжди РЕГУЛЯРНИЙ файл (магія
                        # 0x00051607), ніколи не тека. Раніше is_appledouble
                        # стояв до розрізнення типу запису — тека з іменем
                        # «._X» зникала з паралельного обходу цілком, разом
                        # з усім вмістом.
                        if e.is_dir(follow_symlinks=False):
                            if (_excluded(e.path, e.name)
                                    or excluded_by_profile(
                                        e.path, e.name, directory=True)):
                                ok = False
                                continue
                            subdirs.append(e.path)
                            continue
                        if is_appledouble(e.name):
                            continue  # метадані сусіднього файла, не файл
                        if e.is_symlink():
                            if (profile is not None and excluded_by_profile(
                                    e.path, e.name, symlink=True)):
                                ok = False
                                continue
                            links.append((e.path, os.readlink(e.path)))
                            continue
                        file_stats.append(
                            (e.path, e.stat(follow_symlinks=False)))
                    except OSError as err:
                        message = str(err)
                        if err.errno == errno.ENOENT:
                            if fresh_names is None:
                                try:
                                    fresh_names = os.listdir(dirpath)
                                except OSError:
                                    # Сама тека вже недоступна — нема з чим
                                    # звіряти, лишаємо class "зник" нижче.
                                    fresh_names = []
                            if e.name in fresh_names:
                                # scandir ЩЕ бачить ім'я (свіжим listdir), а
                                # stat падає ENOENT -> не зникнення, а
                                # драйвер, що не відкриває наявний файл.
                                # Файл однаково лишається виключеним
                                # (file_stats без нього, ok=False нижче) —
                                # це стосується ЛИШЕ тексту помилки.
                                message = (
                                    f"{e.path}: перелічений у теці, але "
                                    "macOS-драйвер exFAT не відкриває (fskit)"
                                )
                        errs.append(message)
                        ok = False
        except OSError as err:
            # теку не відкрити: як onerror у os.walk — вона «не відвідана»,
            # без запису в dir_ok, тож батько отруїться відсутнім digest-ом
            errs.append(str(err))
            return dirpath, [], [], [], errs, ok, False
        return dirpath, subdirs, file_stats, links, errs, ok, True

    def walk_parallel() -> bool:
        """BFS-хвилями: пул читає теки хвилі, злиття — тут, в одному потоці.
        visited страхує від повторного візиту (вкладені корені). False =
        скасовано."""
        visited: set[str] = set()
        wave: list[str] = []
        for root in roots:
            root = os.path.abspath(root)
            if not os.path.isdir(root):
                _record_error(res, f"не тека: {root}")
                continue
            if root not in visited:
                visited.add(root)
                wave.append(root)
        with ThreadPoolExecutor(max_workers=walk_threads) as pool:
            while wave:
                next_wave: list[str] = []
                for r in pool.map(scan_one_dir, wave):
                    _wait_if_paused(pause, cancel)
                    if cancel.is_set() or r is None:
                        return False
                    dirpath, subdirs, file_stats, links, errs, ok, readable = r
                    for error in errs:
                        _record_error(res, error)
                    if not readable:
                        # Теку не відкрити взагалі (напр. permission denied):
                        # немає запису в dir_ok — той самий fail-closed
                        # шлях, яким directory_tree_complete/mergeable вже
                        # відмовляють на відсутньому запису; dir_read_failed
                        # тут не потрібен.
                        continue
                    dir_ok[dirpath] = ok
                    if errs:
                        # errs непорожній лише через справжній OSError на
                        # елементі (не через виключення) — див. except-гілку
                        # вище в scan_one_dir.
                        res.dir_read_failed.add(dirpath)
                    dir_files.setdefault(dirpath, [])
                    dir_links[dirpath] = list(links)
                    dir_aliases.setdefault(dirpath, [])
                    if subdirs:
                        dir_children[dirpath] = list(subdirs)
                    for s in subdirs:
                        if s not in visited:
                            visited.add(s)
                            next_wave.append(s)
                    for fp, st in file_stats:
                        if stat_mod.S_ISREG(st.st_mode):
                            take_file(dirpath, fp, st)
                        else:
                            # Непідтримуваний тип запису — справжня
                            # деградація читання, не виключення.
                            dir_ok[dirpath] = False
                            res.dir_read_failed.add(dirpath)
                wave = next_wave
        return True

    completed = walk_parallel() if walk_threads > 1 else walk_sequential()
    return files, completed


def _hash_and_classify_stage(
    files: list[FileInfo], res: ScanResult, cache: "HashCache | None",
    cancel: threading.Event, pause: threading.Event | None, tick,
    adaptive: "throttle.AdaptiveConcurrency | None",
    activity: "throttle.DiskActivity | None",
) -> None:
    """Воронка розмір -> проба -> повний хеш: класифікує знайдені файли у
    res.file_class/class_size/class_paths (дублікати за вмістом) і рахує
    фізичне сховище дубльованих файлів (res.file_alloc/file_storage)."""
    by_size: dict[int, list[FileInfo]] = defaultdict(list)
    for f in files:
        by_size[f.size].append(f)
    uniq_n = [0]

    def uniq(fp: str) -> None:
        uniq_n[0] += 1
        res.file_class[fp] = f"u:{uniq_n[0]}"

    survivors: list[FileInfo] = []
    for size, group in by_size.items():
        if size == 0:
            # Деякі зовнішні файлові системи повертають стабільний розмір 0,
            # але міняють synthetic inode/ctime між двома lstat. Читати
            # нуль байтів немає сенсу: підтверджуємо лише тип+розмір і
            # використовуємо відомий BLAKE3 порожнього вмісту. Missing або
            # файл, що став ненульовим, як і раніше робить дерево неповним.
            zero_key = f"0:{blake3.blake3().hexdigest()}"
            valid: list[FileInfo] = []
            for fi in group:
                try:
                    current = os.lstat(fi.path)
                    if (not stat_mod.S_ISREG(current.st_mode)
                            or current.st_size != 0):
                        raise OSError(
                            errno.ESTALE, "нульовий файл змінився після обходу",
                            fi.path)
                except OSError as error:
                    _record_error(res, error)
                    res.dir_ok[os.path.dirname(fi.path)] = False
                    res.dir_read_failed.add(os.path.dirname(fi.path))
                    uniq(fi.path)
                else:
                    res.file_class[fi.path] = zero_key
                    valid.append(fi)
            if valid:
                res.class_size[zero_key] = 0
                res.class_paths[zero_key] = sorted(fi.path for fi in valid)
        elif len(group) == 1:
            uniq(group[0].path)
        else:
            survivors.extend(group)

    def hash_stage(cands: list[FileInfo], full: bool, phase: str) -> dict[str, list[FileInfo]]:
        out: dict[str, list[FileInfo]] = defaultdict(list)
        total = len(cands)
        done = [0]
        bytes_done = [0]
        lock = threading.Lock()
        kind = "f" if full else "p"
        mib = 1024 * 1024
        total_bytes = sum(fi.size for fi in cands) if full else 0
        total_units = max(1, (total_bytes + mib - 1) // mib) if full else total
        progress_phase = f"{phase} · МіБ" if full else phase

        # ---- кеш: хіти йдуть прямо в out БЕЗ воркера і БЕЗ читання диска -----
        # get_many — один пакетний SELECT на ~500 файлів, а не запит на файл:
        # на 60k+ файлах роздрібні запити самі ставали вузьким місцем (SQLite
        # round-trip дорожчий за сам пошук по PRIMARY KEY).
        promises = cands
        if cache is not None:
            hits = cache.get_many(
                kind,
                [(fi.path, fi.size, fi.mtime_ns, fi.ctime_ns, fi.dev, fi.ino)
                 for fi in cands],
                cancel=cancel,
                pause=pause,
                progress=tick,
            )
            promises = []
            for fi in cands:
                d = hits.get(fi.path)
                if d is None:
                    promises.append(fi)
                else:
                    out[f"{fi.size}:{d}"].append(fi)
                    done[0] += 1
                    if full:
                        bytes_done[0] += fi.size
                        tick(
                            progress_phase,
                            min(total_units, (bytes_done[0] + mib - 1) // mib),
                            total_units,
                        )
                    else:
                        tick(progress_phase, done[0], total_units)

        parallel_files = _hash_file_parallelism(promises, full)
        def work(fi: FileInfo):
            _wait_if_paused(pause, cancel)
            if cancel.is_set():
                return fi, None, None
            reported = [0]

            def file_progress(read_bytes: int, _file_total: int) -> None:
                if not full:
                    return
                with lock:
                    delta = max(0, read_bytes - reported[0])
                    reported[0] = max(reported[0], read_bytes)
                    bytes_done[0] += delta
                    tick(
                        progress_phase,
                        min(total_units, (bytes_done[0] + mib - 1) // mib),
                        total_units,
                    )

            # throttle.AdaptiveConcurrency гейтить
            # ЛИШЕ повне фізичне читання (full=True; пробне читання лишається
            # "highly parallel", як у джерелі) — acquire/release тримають слот
            # рівно навколо _hash_file (читання+хеш; DupScan не розділяє їх на
            # фази, на відміну від джерела, тож простіша, безпечніша адаптація:
            # єдиний узгоджений блок, а не пофазне розділення). adaptive=None
            # (типовий виклик без Блоку T) — гейт цілком пропускається.
            # Активний гейт зберігається в окрему локальну
            # змінну (не bool-прапорець), щоб mypy звужував adaptive/activity
            # у кожній гілці незалежно — bool-прапорець тут не корелюється з
            # None-перевіркою пізніше (той самий клас плутанини, що з
            # root_id() у моделях).
            active_gate = adaptive if full else None
            if active_gate is not None:
                active_gate.acquire(should_stop=cancel.is_set)
                if activity is not None:
                    activity.start_read(time.monotonic())
            try:
                if not proof_is_current(fi, os.lstat(fi.path)):
                    raise OSError(errno.ESTALE, "файл змінився після обходу", fi.path)
                d = _hash_file(
                    fi.path, fi.size, full, cancel, pause,
                    progress=file_progress if full else None,
                    max_threads=_blake3_threads(
                        fi.size, full, parallel_files),
                )
                if not proof_is_current(fi, os.lstat(fi.path)):
                    raise OSError(errno.ESTALE, "файл змінився під час хешування",
                                  fi.path)
                err = None
            except OSError as e:
                d, err = None, str(e)
            finally:
                if active_gate is not None:
                    if activity is not None:
                        activity.end_read(time.monotonic(), fi.size)
                    active_gate.release()
            with lock:
                done[0] += 1
                if full:
                    # Errors/cancellation still complete this progress slot;
                    # correctness is represented separately by errors/partial.
                    bytes_done[0] += max(0, fi.size - reported[0])
                    tick(
                        progress_phase,
                        min(total_units, (bytes_done[0] + mib - 1) // mib),
                        total_units,
                    )
                else:
                    tick(progress_phase, done[0], total_units)
            return fi, d, err

        # НІКОЛИ не кешуємо None (перерваний cancel-read або OSError) — лише
        # справжній щойно обчислений digest, і то одним пакетним записом.
        new_rows: list[tuple] = []
        with ThreadPoolExecutor(max_workers=parallel_files) as pool:
            for fi, d, err in pool.map(work, promises):
                if err is not None:
                    _record_error(res, err)
                    res.dir_ok[os.path.dirname(fi.path)] = False
                    # Файл зник/змінився під час хешування (ESTALE тощо) —
                    # справжній збій читання, не виключення. Не в переліку
                    # задачі А.2 (лінія ~851 у постановці), додано за тим
                    # самим принципом.
                    res.dir_read_failed.add(os.path.dirname(fi.path))
                    uniq(fi.path)
                elif d is None:
                    uniq(fi.path)
                else:
                    out[f"{fi.size}:{d}"].append(fi)
                    if cache is not None:
                        new_rows.append((fi.path, kind, fi.size, fi.mtime_ns,
                                         fi.ctime_ns, fi.dev, fi.ino, d))
        if cache is not None and new_rows:
            cache.put_many(
                new_rows, cancel=cancel, pause=pause, progress=tick)
        return out

    part = hash_stage(survivors, full=False, phase="Порівняння початків")
    finalists: list[FileInfo] = []
    settled: dict[str, list[FileInfo]] = {}
    settled_rows: list[tuple] = []
    for key, group in part.items():
        if len(group) == 1:
            uniq(group[0].path)
        elif group[0].size <= PARTIAL:
            # Проба прочитала файл ЦІЛКОМ (min(PARTIAL, size) == size), тож
            # її BLAKE3 і є повним дайджестом — другий прохід перечитував би
            # ті самі байти. Виміряно на 27k-дереві: −25% часу, −27% читань;
            # SHA-256 складу груп/тек/пар ідентичний дворазовому шляху.
            settled[key] = group
            digest = key.split(":", 1)[1]
            for fi in group:
                settled_rows.append((fi.path, "f", fi.size, fi.mtime_ns,
                                     fi.ctime_ns, fi.dev, fi.ino, digest))
        else:
            finalists.extend(group)
    if cache is not None and settled_rows:
        # сумісність теплого шляху: осілі файли мають kind="f" рядки в кеші
        cache.put_many(settled_rows, cancel=cancel, pause=pause, progress=tick)

    full_groups = hash_stage(finalists, full=True, phase="Повне хешування")
    for key, group in settled.items():
        full_groups.setdefault(key, []).extend(group)
    for key, group in full_groups.items():
        if len(group) == 1:
            uniq(group[0].path)
            continue
        for fi in group:
            res.file_class[fi.path] = key
        res.class_size[key] = group[0].size
        res.class_paths[key] = sorted(fi.path for fi in group)

    # Фізичне сховище ЛИШЕ дубльованих файлів: APFS-клони не мають завищувати
    # «звільнить». Помилка проби -> шлях лишається без id (класична оцінка).
    probe_paths = [p for paths in res.class_paths.values() if len(paths) >= 2
                   for p in paths]
    for index, p in enumerate(probe_paths):
        if index % 256 == 0:
            _wait_if_paused(pause, cancel)
            if cancel.is_set():
                break
        try:
            st_probe = os.lstat(p)
            res.file_alloc[p] = max(0, st_probe.st_blocks * 512)
        except OSError:
            pass
        sid = _storage_id(p)
        if sid is not None:
            res.file_storage[p] = sid


def scan(
    roots: list[str], progress=None, cancel: threading.Event | None = None,
    cache: "HashCache | None" = None, pause: threading.Event | None = None,
    walk_threads: int | None = None, profile: "ScanProfile | None" = None,
    adaptive: "throttle.AdaptiveConcurrency | None" = None,
    activity: "throttle.DiskActivity | None" = None,
) -> ScanResult:
    """*adaptive*/*activity* — необов'язкові,
    типово None. З None поведінка й результат побайтово ідентичні коду до
    Блоку T (гейт просто не викликається). Коли викликач (workers.
    ScanWorker) передає їх, throttle.AdaptiveConcurrency гейтить ЛИШЕ
    фізичне повне читання файла у hash_stage() (_hash_and_classify_stage) —
    не стат/обхід, не пробне читання (джерело: "Partial probes stay highly
    parallel"), і
    структурно НЕ шляхи доказу перед Кошиком (verify_current_file/
    _verified_survivor/snapshot_directory — окремі функції, ніколи не
    проходять через hash_stage)."""
    res = ScanResult()
    roots = _normalize_roots(roots, res.errors)
    res.scanned_roots = list(roots)
    res.errors_total = len(res.errors)
    for root in roots:
        fs_type = _volume_fs_type(root)
        if fs_type in _NETWORK_FS:
            note = (f"мережевий том ({fs_type}): {root} — швидкість обмежена "
                    f"мережею, оцінки часу можуть бути неточні")
            res.advisories.append(note)
            _record_error(res, note)  # видимість у наявному UI помилок
    # Авто-гейт обходу за носієм: зовнішні томи -> WALK_THREADS потоків
    # (ховаємо латентність тек), внутрішній диск -> послідовний (швидший там).
    if walk_threads is None:
        walk_threads = WALK_THREADS if any(_is_external(r) for r in roots) else 1
    cancel = cancel or threading.Event()
    last_tick = [0.0]

    def tick(phase: str, done: int, total: int) -> None:
        now = time.monotonic()
        if progress and (now - last_tick[0] > 0.1 or done == total):
            last_tick[0] = now
            progress(phase, done, total)

    files, completed = _walk_stage(
        roots, res, profile, walk_threads, cancel, pause, tick)
    res.files_seen = len(files)
    res.bytes_seen = sum(f.size for f in files)
    if not completed:
        res.partial = True
        _publish_partial_file_groups(res)
        return res

    _hash_and_classify_stage(
        files, res, cache, cancel, pause, tick, adaptive, activity)

    if cancel.is_set():
        res.partial = True
    try:
        _aggregate(
            res,
            cancel=cancel,
            pause=pause,
            progress=tick,
        )
    except OSError as error:
        if error.errno != errno.ECANCELED:
            raise
        # Cancellation must not leave half-published directory/similarity
        # state. Exact file classes already contain only fully hashed files,
        # so they are the only useful partial result safe to expose.
        res.partial = True
        _publish_partial_file_groups(res)
    tick("Готово", 1, 1)
    return res


def _dir_storage_signature(res: ScanResult, root_dir: str, memo: dict):
    """Мультимножина storage id усіх файлів піддерева (пост-порядок, без
    рекурсії). None = хоч один файл без id -> сім'я невідома (консервативно
    окрема)."""
    stack = [root_dir]
    order: list[str] = []
    while stack:
        d = stack.pop()
        order.append(d)
        stack.extend(res.dir_children.get(d, ()))
    for d in reversed(order):
        if d in memo:
            continue
        ids: list = []
        ok = True
        for p in res.dir_files.get(d, ()):
            sid = res.file_storage.get(p)
            if sid is None:
                ok = False
                break
            ids.append(sid)
        if ok:
            for sub in res.dir_children.get(d, ()):
                sub_sig = memo.get(sub)
                if sub_sig is None:
                    ok = False
                    break
                ids.extend(sub_sig)
        memo[d] = tuple(sorted(ids)) if ok else None
    return memo[root_dir]


def _publish_partial_file_groups(res: ScanResult) -> None:
    """Publish only fully proven exact-file groups for a cancelled scan."""
    groups = [
        FileGroup(res.class_size[class_id], class_id, list(paths))
        for class_id, paths in res.class_paths.items()
        if len(paths) >= 2 and class_id in res.class_size
    ]
    groups.sort(key=lambda group: group.wasted, reverse=True)
    res.file_groups = groups
    res.dir_groups = []
    res.sim_pairs = []


def _aggregate(
    res: ScanResult,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
) -> None:
    """Файлові групи + Merkle-групи папок + подібність — з готових хешів, без I/O."""
    cancel = cancel or threading.Event()

    def check_control() -> None:
        _wait_if_paused(pause, cancel)
        if cancel.is_set():
            raise OSError(errno.ECANCELED, "агрегацію результатів скасовано")

    def report(phase: str, done: int, total: int) -> None:
        if progress is not None:
            progress(phase, done, total)

    check_control()
    report("Групую точні дублікати", 0, len(res.class_paths))
    file_groups = []
    for k, paths in res.class_paths.items():
        if len(paths) < 2:
            continue
        size_k = res.class_size[k]
        # сім'ї сховища: клони діляться екстентами -> одна сім'я;
        # обсяг сім'ї = реально зайняте (st_blocks), не логічний розмір
        fam_alloc: dict = {}
        for p in paths:
            sid = res.file_storage.get(p)
            alloc = res.file_alloc.get(p)
            # кап логічним розміром: блокове округлення не має розганяти
            # оцінку понад розмір файла; розріджені лишаються меншими
            alloc = size_k if alloc is None else min(alloc, size_k)
            fam_key = sid if sid is not None else ("u", p)
            fam_alloc[fam_key] = max(fam_alloc.get(fam_key, 0), alloc)
        families = max(1, min(len(paths), len(fam_alloc)))
        if len(fam_alloc) > 1:
            freeable = sum(fam_alloc.values()) - min(fam_alloc.values())
        else:
            freeable = 0
        file_groups.append(FileGroup(size_k, k, list(paths),
                                     families, freeable))
    file_groups.sort(key=lambda g: g.wasted, reverse=True)
    check_control()

    all_dirs = (set(res.dir_ok) | set(res.dir_files) | set(res.dir_children)
                | set(res.dir_links) | set(res.dir_aliases))
    order = sorted(all_dirs, key=lambda d: d.count(os.sep), reverse=True)
    report("Групую структуру тек", 0, len(order))
    parent_of = {d: os.path.dirname(d) for d in all_dirs}

    def parent(path: str) -> str:
        known = parent_of.get(path)
        if known is None:
            known = os.path.dirname(path)
            parent_of[path] = known
        return known
    dir_digest: dict[str, str | None] = {}
    dir_bytes: dict[str, int] = {}
    dir_count: dict[str, int] = {}
    dir_classes: dict[str, Counter] = {}

    nfc = unicodedata.normalize
    for directory_index, d in enumerate(order):
        if directory_index % 256 == 0:
            check_control()
            report("Групую структуру тек", directory_index, len(order))
        ok = res.dir_ok.get(d, True)
        h = blake3.blake3()
        total_b, total_n = 0, 0
        classes: Counter = Counter()
        for file_index, fp in enumerate(sorted(
            res.dir_files.get(d, []),
            key=lambda p: nfc("NFC", os.path.basename(p)),
        )):
            if file_index % 2048 == 0:
                check_control()
            fi = res.file_meta[fp]
            total_b += fi.size
            total_n += 1
            c = res.file_class.get(fp)
            if c is None:
                ok = False
                continue
            nm = nfc("NFC", os.path.basename(fp))
            h.update(b"F" + nm.encode("utf-8", "surrogatepass") + b"\0" + c.encode() + b"\0")
            if not c.startswith("u:"):
                classes[c] += 1
        if ok:
            for lp, target in sorted(
                res.dir_links.get(d, []),
                key=lambda item: nfc("NFC", os.path.basename(item[0])),
            ):
                nm = nfc("NFC", os.path.basename(lp))
                h.update(b"L" + nm.encode("utf-8", "surrogatepass") + b"\0"
                         + target.encode("utf-8", "surrogatepass") + b"\0")
        if ok:
            for alias, canonical in sorted(
                res.dir_aliases.get(d, []),
                key=lambda item: nfc("NFC", os.path.basename(item[0])),
            ):
                c = res.file_class.get(canonical)
                if c is None:
                    ok = False
                else:
                    nm = nfc("NFC", os.path.basename(alias))
                    h.update(b"F" + nm.encode("utf-8", "surrogatepass") + b"\0"
                             + c.encode() + b"\0")
                total_n += 1
        for sub in sorted(res.dir_children.get(d, []),
                          key=lambda p: nfc("NFC", os.path.basename(p))):
            cd = dir_digest.get(sub)
            if cd is None:
                ok = False
            else:
                nm = nfc("NFC", os.path.basename(sub))
                h.update(b"D" + nm.encode("utf-8", "surrogatepass") + b"\0" + cd.encode() + b"\0")
            total_b += dir_bytes.get(sub, 0)
            total_n += dir_count.get(sub, 0)
            classes += dir_classes.get(sub, Counter())
        dir_digest[d] = h.hexdigest() if ok else None
        dir_bytes[d] = total_b
        dir_count[d] = total_n
        dir_classes[d] = classes

    by_dg: dict[str, list[str]] = defaultdict(list)
    for d in all_dirs:
        # Одне зчитування замість дублю get()+[] — раніше
        # guard і фактичне індексування були двома незалежними викликами,
        # mypy не бачив, що другий гарантовано non-None після першого.
        digest = dir_digest.get(d)
        if digest:
            by_dg[digest].append(d)
    grouped_dirs = {d for ds in by_dg.values() if len(ds) >= 2 for d in ds}

    def covered(d: str) -> bool:
        p = parent(d)
        while len(p) > 1:
            if p in grouped_dirs:
                return True
            p = parent(p)
        return False

    dir_groups = []
    for group_index, ds in enumerate(by_dg.values()):
        if group_index % 256 == 0:
            check_control()
        kept = sorted(d for d in ds if not covered(d))
        if len(kept) >= 2:
            sig_memo: dict = {}
            sigs: set = set()
            unknown = 0
            for d in kept:
                sig = _dir_storage_signature(res, d, sig_memo)
                if sig is None:
                    unknown += 1
                else:
                    sigs.add(sig)
            dir_families = max(1, min(len(kept), len(sigs) + unknown))
            dir_groups.append(
                DirGroup(dir_bytes[kept[0]], dir_count[kept[0]], kept,
                         dir_families))
    dir_groups.sort(key=lambda g: g.wasted, reverse=True)

    # ---- подібність ------------------------------------------------------------
    report("Шукаю подібні теки", 0, len(res.dir_files))
    cls_dirs: dict[str, list[str]] = defaultdict(list)
    for directory_index, (d, fps) in enumerate(res.dir_files.items()):
        if directory_index % 256 == 0:
            check_control()
            report(
                "Шукаю подібні теки",
                directory_index,
                len(res.dir_files),
            )
        seen_here = set()
        for fp in fps:
            c = res.file_class.get(fp, "")
            if c and not c.startswith("u:") and c not in seen_here:
                seen_here.add(c)
                cls_dirs[c].append(d)

    pair_seen: set[tuple[str, str]] = set()
    base_pair_seen: set[tuple[str, str]] = set()
    best: list[tuple[int, float, str, str, tuple[str, ...]]] = []
    duplicate_bytes = {
        directory: sum(res.class_size[key] * count
                       for key, count in classes.items())
        for directory, classes in dir_classes.items()
    }

    def aligned_ancestors(a: str, b: str):
        """Yield a bounded chain of direct parents and their aligned ancestors.

        A duplicate inside A/photos and B/photos is evidence for both the
        immediate pair and A↔B. Walking upward together avoids an all-dirs
        Cartesian product while restoring large, useful folder candidates.
        """
        while a in dir_classes and b in dir_classes:
            yield tuple(sorted((a, b)))
            parent_a = parent(a)
            parent_b = parent(b)
            if (parent_a == a or parent_b == b
                    or parent_a == parent_b):
                return
            a, b = parent_a, parent_b

    def consider(a: str, b: str) -> None:
        if a == b or (a, b) in pair_seen:
            return
        pair_seen.add((a, b))
        if (a, b) in res.ignored_pairs:
            return
        nested = (
            a == os.sep or b == os.sep
            or a.startswith(b.rstrip(os.sep) + os.sep)
            or b.startswith(a.rstrip(os.sep) + os.sep)
        )
        # Порівнюємо Merkle digest напряму: це охоплює й exact-пари,
        # приховані як вкладені, без квадратичного set усіх комбінацій.
        if (nested or (
                dir_digest.get(a) is not None
                and dir_digest.get(a) == dir_digest.get(b))):
            return
        ca, cb = dir_classes.get(a, Counter()), dir_classes.get(b, Counter())
        if (len(best) >= MAX_PAIRS
                and min(duplicate_bytes.get(a, 0),
                        duplicate_bytes.get(b, 0)) < best[0][0]):
            return
        smaller, larger = (ca, cb) if len(ca) <= len(cb) else (cb, ca)
        common = tuple(key for key in smaller if key in larger)
        shared = sum(res.class_size[k] * min(ca[k], cb[k]) for k in common)
        ta, tb = dir_bytes.get(a, 0), dir_bytes.get(b, 0)
        if shared <= 0 or ta + tb == 0:
            return
        percent = round(200.0 * shared / (ta + tb), 1)
        item = (shared, percent, a, b, common)
        if len(best) < MAX_PAIRS:
            heappush(best, item)
        elif item[:4] > best[0][:4]:
            heapreplace(best, item)

    for class_index, ds in enumerate(cls_dirs.values()):
        if class_index % 128 == 0:
            check_control()
        if len(ds) < 2:
            continue
        if len(ds) > FANOUT_CAP:
            # Dropping a common class entirely hides large archive/backup
            # candidates when the same photo exists in many folders. Sample
            # deterministically across the full path range instead; candidate
            # scoring still uses complete directory counters and remains
            # bounded to FANOUT_CAP².
            ordered = sorted(set(ds))
            last = len(ordered) - 1
            ds = [
                ordered[index * last // (FANOUT_CAP - 1)]
                for index in range(FANOUT_CAP)
            ]
        for i in range(len(ds)):
            if i % 64 == 0:
                check_control()
            for j in range(i + 1, len(ds)):
                a, b = sorted((ds[i], ds[j]))
                if (a, b) in base_pair_seen:
                    continue
                base_pair_seen.add((a, b))
                for ancestor_a, ancestor_b in aligned_ancestors(a, b):
                    consider(ancestor_a, ancestor_b)

    def first_under(paths: list[str], root: str) -> str | None:
        prefix = root.rstrip(os.sep) + os.sep
        pos = bisect_left(paths, prefix)
        if pos < len(paths) and paths[pos].startswith(prefix):
            return paths[pos]
        return None

    pairs: list[SimPair] = []
    for pair_index, (shared, percent, a, b, common) in enumerate(
        sorted(best, reverse=True)
    ):
        if pair_index % 64 == 0:
            check_control()
        rows: list[tuple[int, str, str]] = []
        total_rows = 0
        for k in sorted(common, key=lambda key: -res.class_size[key]):
            paths = res.class_paths.get(k, [])
            pa, pb = first_under(paths, a), first_under(paths, b)
            if pa and pb:
                total_rows += 1
                if len(rows) < MAX_SHARED_ROWS:
                    rows.append((res.class_size[k], pa, pb))
        pairs.append(SimPair(a, b, percent, shared, rows, total_rows))
    check_control()
    # Publish derived presentation state only after a complete aggregation.
    res.file_groups = file_groups
    res.dir_groups = dir_groups
    res.sim_pairs = pairs


def snapshot_directory(
    path: str,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
) -> tuple[str, int, int]:
    """Побудувати свіжий повний Merkle-маніфест дерева без виключень.

    Кожен regular-файл читається повністю через verify_current_file(), symlink
    входить як текст target, інші типи та будь-яка помилка переривають
    перевірку. Саме цей доказ використовується перед видаленням цілої папки.
    """
    nfc = unicodedata.normalize
    hardlinks: dict[tuple[int, int], tuple[str, int]] = {}
    cancel = cancel or threading.Event()
    completed_bytes = [0]
    completed_files = [0]

    def visit(directory: str) -> tuple[str, int, int]:
        _wait_if_paused(pause, cancel)
        if cancel.is_set():
            raise OSError(errno.ECANCELED, "перевірку теки скасовано", path)
        st = os.lstat(directory)
        if not stat_mod.S_ISDIR(st.st_mode) or stat_mod.S_ISLNK(st.st_mode):
            raise OSError(errno.ENOTDIR, "очікувалась звичайна тека", directory)
        try:
            with os.scandir(directory) as it:
                entries = sorted(list(it), key=lambda e: nfc("NFC", e.name))
        except OSError:
            raise
        h = blake3.blake3()
        total_b = total_n = 0
        for entry in entries:
            _wait_if_paused(pause, cancel)
            if cancel.is_set():
                raise OSError(
                    errno.ECANCELED, "перевірку теки скасовано", entry.path)
            st_e = entry.stat(follow_symlinks=False)
            if (stat_mod.S_ISREG(st_e.st_mode) and is_appledouble(entry.name)
                    and _appledouble_sibling_exists(directory, entry.name)):
                # Доказ мусить бачити те саме, що й гейт Кошика, інакше
                # маніфест ніколи не збігається з очікуваним і гейт
                # відмовляє завжди. Пропуск лише для РЕГУЛЯРНОГО файла з
                # байтовим сусідом X —
                # тека «._X» і самотній «._secret» БЕЗ сусіда більше не
                # зникають зі знімка мовчки.
                continue
            name = nfc("NFC", entry.name).encode("utf-8", "surrogatepass")
            if stat_mod.S_ISLNK(st_e.st_mode):
                target = os.readlink(entry.path).encode("utf-8", "surrogatepass")
                h.update(b"L" + name + b"\0" + target + b"\0")
            elif stat_mod.S_ISREG(st_e.st_mode):
                inode = (st_e.st_dev, st_e.st_ino)
                cached = hardlinks.get(inode) if st_e.st_nlink > 1 else None
                if cached is None:
                    base_bytes = completed_bytes[0]

                    def file_progress(read_bytes: int, _total: int) -> None:
                        if progress is not None:
                            progress(
                                entry.path, base_bytes + read_bytes,
                                completed_files[0])

                    digest, after = verify_current_file(
                        entry.path, cancel=cancel, pause=pause,
                        progress=file_progress)
                    size = after.st_size
                    completed_bytes[0] += size
                    if after.st_nlink > 1:
                        hardlinks[inode] = (digest, size)
                    total_b += size
                else:
                    digest, size = cached
                key = f"{size}:{digest}".encode()
                h.update(b"F" + name + b"\0" + key + b"\0")
                total_n += 1
                completed_files[0] += 1
                if progress is not None:
                    progress(
                        entry.path, completed_bytes[0], completed_files[0])
            elif stat_mod.S_ISDIR(st_e.st_mode):
                digest, sub_b, sub_n = visit(entry.path)
                h.update(b"D" + name + b"\0" + digest.encode() + b"\0")
                total_b += sub_b
                total_n += sub_n
            else:
                raise OSError(errno.ENOTSUP, "непідтримуваний тип файла", entry.path)
        return h.hexdigest(), total_b, total_n

    return visit(os.path.abspath(path))


def snapshot_directory_state(
    path: str,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
) -> tuple[str, int, int]:
    """МЕТАДАНИЙ знімок дерева: TOCTOU-замок без читання вмісту.

    Ловить будь-яку зміну під час операції: додано, видалено, перейменовано,
    замінено, відредаговано, змінено права чи ціль symlink. Вміст доводять
    пофайлові докази (proof_kind/proof_is_current) безпосередньо перед дією,
    тому повне перечитування цілої теки двічі було надлишковим: на парі тек
    власника це коштувало 2×19.7 ГБ.

    НЕ придатний для порівняння РІЗНИХ тек між собою (містить inode і час) —
    для цього є snapshot_directory().
    """
    nfc = unicodedata.normalize
    cancel = cancel or threading.Event()
    files = [0]
    total_bytes = [0]

    def visit(directory: str) -> tuple[str, int, int]:
        _wait_if_paused(pause, cancel)
        if cancel.is_set():
            raise OSError(errno.ECANCELED, "перевірку теки скасовано", path)
        st = os.lstat(directory)
        if not stat_mod.S_ISDIR(st.st_mode) or stat_mod.S_ISLNK(st.st_mode):
            raise OSError(errno.ENOTDIR, "очікувалась звичайна тека", directory)
        with os.scandir(directory) as it:
            entries = sorted(list(it), key=lambda e: nfc("NFC", e.name))
        h = blake3.blake3()
        total_b = total_n = 0
        for entry in entries:
            _wait_if_paused(pause, cancel)
            if cancel.is_set():
                raise OSError(
                    errno.ECANCELED, "перевірку теки скасовано", entry.path)
            st_e = entry.stat(follow_symlinks=False)
            if (stat_mod.S_ISREG(st_e.st_mode) and is_appledouble(entry.name)
                    and _appledouble_sibling_exists(directory, entry.name)):
                # Системний шум: macOS сама переписує супутник (напр. оновлює
                # provenance), і TOCTOU-замок спрацьовував би на змінах, яких
                # користувач не робив. Пропуск лише для РЕГУЛЯРНОГО файла з
                # байтовим сусідом X — тека «._X» і самотній
                # «._secret» БЕЗ сусіда більше не невидимі цьому замку.
                continue
            name = nfc("NFC", entry.name).encode("utf-8", "surrogatepass")
            if stat_mod.S_ISLNK(st_e.st_mode):
                target = os.readlink(entry.path).encode("utf-8", "surrogatepass")
                h.update(b"L" + name + b"\0" + target + b"\0")
            elif stat_mod.S_ISREG(st_e.st_mode):
                identity = (
                    st_e.st_size, st_e.st_mtime_ns, st_e.st_ctime_ns,
                    st_e.st_dev, st_e.st_ino, st_e.st_nlink,
                    st_e.st_mode, st_e.st_uid, st_e.st_gid,
                )
                h.update(b"F" + name + b"\0"
                         + repr(identity).encode("utf-8") + b"\0")
                total_b += st_e.st_size
                total_n += 1
                files[0] += 1
                total_bytes[0] += st_e.st_size
                if progress is not None:
                    progress(entry.path, total_bytes[0], files[0])
            elif stat_mod.S_ISDIR(st_e.st_mode):
                digest, sub_b, sub_n = visit(entry.path)
                h.update(b"D" + name + b"\0" + digest.encode() + b"\0")
                total_b += sub_b
                total_n += sub_n
            else:
                raise OSError(
                    errno.ENOTSUP, "непідтримуваний тип файла", entry.path)
        return h.hexdigest(), total_b, total_n

    return visit(os.path.abspath(path))


def directory_tree_complete(res: ScanResult, root: str) -> bool:
    """True only when a fresh scan read every directory below ``root``.

    This is intentionally about traversal completeness, not identity. Merge
    operations still re-read every moved file and every file considered for
    the final Trash step. It lets the UI refuse a selective pair merge when a
    permission error, profile exclusion, unsupported entry, or unreadable
    child would make the two-folder view incomplete.
    """
    root = os.path.abspath(root)
    seen: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory in seen:
            continue
        seen.add(directory)
        if res.dir_ok.get(directory) is not True:
            return False
        for child in res.dir_children.get(directory, ()):
            try:
                if os.path.commonpath((root, child)) != root:
                    return False
            except ValueError:
                return False
            pending.append(child)
    return True


def directory_tree_mergeable(res: ScanResult, root: str) -> bool:
    """True when nothing under ``root`` genuinely failed to read.

    directory_tree_complete backs the Merkle-style proof that two folders
    are byte-identical, so it must fail on ANY unread content — a deliberate
    exclusion (EXCLUDE_NAMES service directories, a scan profile filter) is
    indistinguishable from a real read failure there, by design. Preparing a
    selective A/B merge is a different question: content the scan chose not
    to read is simply never proposed for transfer, so it does not make the
    merge unsafe by itself. What DOES make it unsafe is a directory this
    scan could not actually confirm — either a real read failure recorded in
    dir_read_failed, or a directory the walk never reached at all (no
    dir_ok entry whatsoever — e.g. permission denied opening it after its
    parent already listed it as a child). The latter has no positive proof
    of a complete read, so it fails closed exactly like
    directory_tree_complete does for that same case.
    """
    root = os.path.abspath(root)
    seen: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory in seen:
            continue
        seen.add(directory)
        ok = res.dir_ok.get(directory)
        if ok is None:
            return False
        if ok is False and directory in res.dir_read_failed:
            return False
        for child in res.dir_children.get(directory, ()):
            try:
                if os.path.commonpath((root, child)) != root:
                    return False
            except ValueError:
                return False
            pending.append(child)
    return True


def shared_side(res: ScanResult, a: str, b: str, remove_from_a: bool = True) -> list[str]:
    """ПОВНИЙ список файлів обраного боку пари, чий вміст (клас) присутній і
    на другому боці. Позиційно-незалежно: рахує клас вмісту, не імена/шляхи.
    Це джерело для масового «Прибрати спільне з X» — НЕ капнуті рядки показу."""
    src, other = (a, b) if remove_from_a else (b, a)
    src_p, other_p = src + os.sep, other + os.sep
    other_classes = {
        c for p, c in res.file_class.items()
        if not c.startswith("u:") and p.startswith(other_p)
    }
    return sorted(
        p for p, c in res.file_class.items()
        if not c.startswith("u:") and p.startswith(src_p) and c in other_classes
    )


def pair_diff(
    res: ScanResult, a: str, b: str, cap: int | None = MAX_SHARED_ROWS
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], int, int]:
    """Різниця пари: файли ЛИШЕ в A і ЛИШЕ в B (включно з унікальними).
    Повертає (rows_a, rows_b, total_a, total_b): rows — (size, path),
    найбільші перші, капнуті; totals — повні кількості. Обхід — по
    in-memory dir_files/dir_children, без диска."""

    def files_under(root: str) -> list[str]:
        out: list[str] = []
        stack = [root]
        visited: set[str] = set()
        while stack:
            d = stack.pop()
            if d in visited:
                continue
            visited.add(d)
            out.extend(res.dir_files.get(d, ()))
            stack.extend(res.dir_children.get(d, ()))
        return out

    fa, fb = files_under(a), files_under(b)
    ca = {res.file_class.get(p) for p in fa}
    cb = {res.file_class.get(p) for p in fb}

    def only(files: list[str], other: set) -> tuple[list[tuple[int, str]], int]:
        sel = [p for p in files if res.file_class.get(p) not in other]
        rows = sorted(
            ((res.file_meta[p].size, p) for p in sel if p in res.file_meta),
            reverse=True,
        )
        return (rows[:cap] if cap is not None else rows), len(sel)

    rows_a, na = only(fa, cb)
    rows_b, nb = only(fb, ca)
    return rows_a, rows_b, na, nb


def validate_merge_roots(src_dir: str, dst_dir: str) -> tuple[str, str]:
    """Return absolute disjoint merge roots, rejecting aliases and nesting."""
    src_dir = os.path.abspath(src_dir)
    dst_dir = os.path.abspath(dst_dir)
    if os.path.islink(src_dir) or os.path.islink(dst_dir):
        raise ValueError(
            "теки злиття не можуть бути кореневими символічними посиланнями"
        )
    # A merge between aliases of the same tree, or between an ancestor and a
    # descendant, can recursively move data into itself. Reject it centrally:
    # this protects live scans, imported sessions and non-UI callers alike.
    src_physical = os.path.realpath(src_dir)
    dst_physical = os.path.realpath(dst_dir)
    try:
        common = os.path.commonpath((src_physical, dst_physical))
    except ValueError as error:
        raise ValueError("джерело й ціль злиття несумісні") from error
    if common in (src_physical, dst_physical):
        raise ValueError(
            "джерело й ціль злиття мають бути різними невкладеними теками"
        )
    return src_dir, dst_dir


def merge_plan(
    res: ScanResult, src_dir: str, dst_dir: str, with_summary: bool = False
):
    """План злиття: усе, що НЕ доказано як дублікат, переноситься в dst_dir.

    Три категорії кандидатів:
      unique    — файли, чий контент-клас існує лише в джерелі;
      uncovered — файли на диску джерела БЕЗ доказу дубліката (не охоплені
                  профілем скану, наприклад менші за min_size);
      symlinks  — посилання: переносяться як посилання, вміст не читається.
    Доказані дублікати лишаються в джерелі — саме вони дозволяють потім
    відправити всю теку в Кошик. Без цієї повноти крок Кошика відмовляв на
    кожному неохопленому файлі (реальний кейс: 592 файли < 1 МіБ).

    Повертає ([(size, src, rel)], total) або, за with_summary, ще й лічильники.
    """
    src_dir, dst_dir = validate_merge_roots(src_dir, dst_dir)
    _ra, rows, _na, _nb = pair_diff(res, dst_dir, src_dir, cap=None)
    prefix = src_dir + os.sep
    # Стан скану тримає realpath-шляхи (корені нормалізуються), а обхід диска
    # може дати /var замість /private/var. Без спільної нормалізації доказ не
    # знаходився і ДОКАЗАНІ дублікати теж потрапляли в план переносу.
    proven = {
        os.path.realpath(path)
        for path in res.file_class
        if proof_kind(res, path) == FULL_PROOF
    }
    summary = {"unique": 0, "uncovered": 0, "symlinks": 0}
    seen: set[str] = set()
    plan: list[tuple[int, str, str]] = []

    def add(path: str, size: int, bucket: str) -> None:
        path = os.path.abspath(path)
        if path in seen:
            return
        if os.path.commonpath((src_dir, path)) != src_dir:
            raise ValueError(f"небезпечний шлях у плані злиття: {path}")
        rel = os.path.relpath(path, src_dir)
        if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
            raise ValueError(f"шлях виходить за межі джерела: {path}")
        seen.add(path)
        summary[bucket] += 1
        plan.append((size, path, rel))

    for size, p in rows:
        add(p, size, "unique")

    # Реальний диск: усе без доказу дубліката теж мусить переїхати.
    for dirpath, _dirnames, filenames in os.walk(src_dir, followlinks=False):
        for name in filenames:
            if is_appledouble(name):
                # Ядро переносить супутник разом із його файлом; окремий рядок
                # плану дійшов би до вже перенесеного шляху і впав ENOENT.
                continue
            p = os.path.join(dirpath, name)
            if not p.startswith(prefix):
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat_mod.S_ISLNK(st.st_mode):
                add(p, 0, "symlinks")
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            if (proof_kind(res, p) == FULL_PROOF
                    or os.path.realpath(p) in proven):
                continue  # доказаний дублікат лишається в джерелі
            add(p, st.st_size, "uncovered")

    total = sum(s for s, _p, _r in plan)
    if with_summary:
        return plan, total, summary
    return plan, total


def recompute(
    res: ScanResult,
    removed: set[str],
    cancel: threading.Event | None = None,
) -> bool:
    """Bulk-prune missing paths, then atomically publish one rebuilt state.

    Parent-set lookup makes directory removal O(paths × depth), while every
    class/directory list is filtered once. Return ``False`` only when cancelled
    before publication; the original result then remains untouched.
    """
    cancel = cancel or threading.Event()
    removed_dirs = {
        os.path.normpath(os.path.abspath(path))
        for path in removed
        if path not in res.file_meta
    }
    gone_files = {path for path in removed if path in res.file_meta}

    def under_removed(path: str) -> bool:
        current = os.path.normpath(path)
        while True:
            if current in removed_dirs:
                return True
            parent = os.path.dirname(current)
            if parent == current:
                return False
            current = parent

    if removed_dirs:
        gone_files.update(path for path in res.file_meta if under_removed(path))
    if cancel.is_set():
        return False

    file_meta = {
        path: info for path, info in res.file_meta.items()
        if path not in gone_files
    }
    file_class = {
        path: class_id for path, class_id in res.file_class.items()
        if path in file_meta
    }
    class_paths = {
        class_id: kept
        for class_id, paths in res.class_paths.items()
        if (kept := [path for path in paths if path in file_meta])
    }
    class_size = {
        class_id: size for class_id, size in res.class_size.items()
        if class_id in class_paths
    }
    all_dirs = (
        set(res.dir_ok)
        | set(res.dir_files)
        | set(res.dir_children)
        | set(res.dir_links)
        | set(res.dir_aliases)
    )
    kept_dirs = {
        directory for directory in all_dirs if not under_removed(directory)
    }
    dir_ok = {
        directory: ok for directory, ok in res.dir_ok.items()
        if directory in kept_dirs
    }
    dir_files = {
        directory: [path for path in paths if path in file_meta]
        for directory, paths in res.dir_files.items()
        if directory in kept_dirs
    }
    dir_children = {
        directory: [
            child for child in children
            if child in kept_dirs and not under_removed(child)
        ]
        for directory, children in res.dir_children.items()
        if directory in kept_dirs
    }
    dir_links = {
        directory: list(links)
        for directory, links in res.dir_links.items()
        if directory in kept_dirs
    }
    dir_aliases = {
        directory: [
            (alias, canonical)
            for alias, canonical in aliases
            if canonical in file_meta and not under_removed(alias)
        ]
        for directory, aliases in res.dir_aliases.items()
        if directory in kept_dirs
    }
    ignored_pairs = {
        pair for pair in res.ignored_pairs
        if not under_removed(pair[0]) and not under_removed(pair[1])
    }
    if cancel.is_set():
        return False

    rebuilt = ScanResult(
        files_seen=len(file_meta),
        bytes_seen=sum(info.size for info in file_meta.values()),
        errors=list(res.errors),
        errors_total=res.errors_total,
        partial=res.partial,
        live=res.live,
        file_meta=file_meta,
        file_class=file_class,
        class_size=class_size,
        class_paths=class_paths,
        dir_files=dir_files,
        dir_children=dir_children,
        dir_links=dir_links,
        dir_aliases=dir_aliases,
        dir_ok=dir_ok,
        ignored_pairs=ignored_pairs,
        dataless_skipped=res.dataless_skipped,
        advisories=list(res.advisories),
        # фізичне сховище живих файлів — інакше recompute «забуде» про
        # клони і знову завищить «звільнить» (впіймано тестом)
        file_storage={p: sid for p, sid in res.file_storage.items()
                      if p in file_meta},
        file_alloc={p: a for p, a in res.file_alloc.items()
                    if p in file_meta},
    )
    try:
        _aggregate(rebuilt, cancel=cancel)
    except OSError as error:
        if error.errno == errno.ECANCELED:
            return False
        raise
    if cancel.is_set():
        return False

    # Publish only one internally consistent snapshot.
    for field_name in ScanResult.__dataclass_fields__:
        setattr(res, field_name, getattr(rebuilt, field_name))
    return True
