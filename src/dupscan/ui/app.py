"""DupScan — дублікати файлів, дублікати папок, подібність папок (macOS).

UX-правила: теки в списку мають чекбокси і контекстне меню; теки можна
перетягнути з Finder або обрати кілька одразу в діалозі; жодного нав'язаного
«кіпера» — єдиний захист: у групі неможливо позначити ВСІ елементи; сортування
за розміром/датами; при зникненні файлів (Кошик тут або видалення у Finder)
усі три вкладки перераховуються автоматично.
"""

from __future__ import annotations

import json
import hashlib
import heapq
import errno
import os
import re
import stat as stat_mod
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from typing import Callable, Container, cast
from PySide6.QtCore import (
    QAbstractTableModel, QDir, QModelIndex, QSettings,
    QSortFilterProxyModel, Qt, QThread, QTimer, Signal,
)
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFileSystemModel, QFormLayout, QFrame,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy, QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter,
    QStackedWidget, QStyle, QTabWidget, QTableView, QTextBrowser, QTreeView,
    QVBoxLayout, QWidget,
)

import dupscan.infra.cache as cache  # noqa: F401 — тести патчать app.cache.HashCache.open
import dupscan.domain.clusters as clusters
import dupscan.domain.core as core
from dupscan.format import human, when  # noqa: E402 — спільні форматери
import dupscan.infra.diagnostics as diagnostics
import dupscan.infra.fsops as fsops
import dupscan.ui.perceptual as perceptual
import dupscan.infra.preferences as preferences
import dupscan.domain.product as product
import dupscan.infra.reports as reports
from dupscan.ui.clusters_tab_controller import ClustersTabController
from dupscan.ui.folder_comparison_controller import FolderComparisonController
from dupscan.ui.perceptual_tab_controller import PerceptualTabController
from dupscan.ui.problems_tab_controller import ProblemsTabController
from dupscan.ui.removal_history_controller import RemovalHistoryController
from dupscan.ui.reports_controller import ReportsController
from dupscan.ui.settings_controller import SettingsController
import dupscan.ui.scale_ui as scale_ui
import dupscan.infra.session as session
import dupscan.infra.storage_guard as storage_guard
import dupscan.infra.updates as updates
import dupscan.ui.workflow_ui as workflow_ui
from dupscan.version import DISPLAY_NAME, VARIANT_BADGE, VARIANT_NAME, VERSION

__version__ = VERSION
_TRASH_CONTEXT = threading.local()


def _crash_log_path() -> str:
    base = os.environ.get("DUPSCAN_DATA_DIR") or os.path.expanduser(
        "~/Library/Application Support/DupScan")
    return os.path.join(base, "crash.log")


def _read_crash_log() -> str:
    try:
        with open(_crash_log_path(), encoding="utf-8", errors="replace") as handle:
            return handle.read(512 * 1024)
    except OSError:
        return ""


def install_crash_handler() -> None:
    """Persist a small redacted traceback for explicit diagnostics export."""
    previous = sys.excepthook

    def handle(exc_type, value, tb):
        try:
            rendered = "".join(traceback.format_exception(exc_type, value, tb))
            rendered = diagnostics.redact_sensitive_text(rendered)
            payload = (f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n" + rendered)[-512 * 1024:]
            path = _crash_log_path()
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, payload.encode("utf-8", "replace"))
            finally:
                os.close(descriptor)
        except Exception:  # noqa: BLE001 — never recurse from an exception hook
            pass
        previous(exc_type, value, tb)

    sys.excepthook = handle


def storage_amount(n: int) -> str:
    exact = f"{max(0, int(n)):,}".replace(",", " ")
    return f"{human(max(0, n))} ({exact} байтів)"


def _problem_count(result: core.ScanResult | None) -> int:
    if result is None:
        return 0
    return max(len(result.errors), getattr(result, "errors_total", 0))


_PATH_IN_MESSAGE_RE = re.compile(r"""/[^'"]*?(?=: |['"]|$)""")


def _first_path_in_message(message: str) -> str | None:
    """Обережний парсер шляху з тексту помилки для ПКМ «Показати у Finder»
    на вкладці «Проблеми» (те саме джерело шляху для fix_name_to_nfc,
    тож коректність тут не лише косметична). Навмисно
    БЕЗ os.path.exists — це I/O, недоречне синхронно в GUI-потоці; сам
    reveal()/_open_result_path безпечний і на шляху, який насправді не
    існує.

    Межа шляху — «: » (двокрапка-пробіл, формат КОЖНОГО повідомлення в
    core.py/fsops.py: f"{path}: {reason}"), лапка чи кінець рядка — НЕ
    будь-який пробіл: справжні шляхи macOS часто містять пробіли («Мої
    документи», «My Documents»), і старий варіант (не-жадібний до першого
    whitespace) обрізав їх на першому internal-пробілі, віддаючи хибний
    dirpath/name.
    """
    match = _PATH_IN_MESSAGE_RE.search(message)
    if not match:
        return None
    return match.group(0).rstrip(".,;:)]}»")


# ---- Автофікс імен — лише на вкладці «Проблеми» ---------------------------
#
# Точні назви категорій, як їх повертає product.classify_problem (немає
# спільних іменованих констант у product.py — той самий ad-hoc підхід, що
# _update_result_quality/ProblemsDialog уже застосовують у цьому файлі).
# ЛИШЕ ці дві: «фантом» (fskit бачить, не відкриває — саме NFD-кодування
# підозріле) і «зник» (міг зникнути через те саме кодування). Решта
# категорій (доступ/носій/читання) — не про імена, пропонувати фікс там
# було б оманливо.
_NAMEFIX_ELIGIBLE_CATEGORIES = frozenset({
    "Драйвер не відкриває файл", "Елемент зник",
})


def _namefix_status_text(outcome: str, detail: str) -> str:
    """Текст статус-рядка після fsops.fix_name_to_nfc — по одній фразі на
    ЧЕСНИЙ вердикт (жодного видає-за-успіх). Чиста функція: без Qt,
    легко тестується окремо від GUI."""
    if outcome == "fixed":
        return f"Ім'я полагоджено: {detail}. Повторіть сканування."
    if outcome == "phantom":
        return (
            "Драйвер не адресує запис навіть для перейменування — "
            "локально не полагодити; скопіюйте файл через інший комп'ютер.")
    if outcome == "exists":
        return "Файл з канонічним іменем уже існує поруч — нічого не змінено."
    if outcome == "already-nfc":
        return "Ім'я вже канонічне — причина не в кодуванні."
    if outcome == "protected":
        return "Шлях у теці-еталоні — перейменування заборонено."
    return f"Помилка перейменування: {detail}" if detail else "Помилка перейменування."


def _move_result_message(moved_count: int, errors: list[str]) -> str:
    """Чесний підсумок часткового переносу для попапа злиття.

    Раніше текст безумовно закінчувався «Решту перенесено», навіть коли не
    перенеслось нічого (moved_count == 0) — цифра реально перенесених у
    попап не потрапляла зовсім. Тепер вона є завжди, а фрази про «решту»
    немає.
    """
    head = (
        f"Перенесено {moved_count} файл(ів), не перенесено {len(errors)}."
        if moved_count else
        f"Жодного файла не перенесено ({len(errors)} з помилкою).")
    return (
        f"{head}\n\nПерша помилка:\n{errors[0]}\n\n"
        "Теку-джерело лишено на місці.")


def _accessible(widget, object_name: str, name: str, description: str = ""):
    """Give Qt/macOS accessibility and UI tests one stable control contract."""
    widget.setObjectName(object_name)
    widget.setAccessibleName(name)
    if description:
        widget.setAccessibleDescription(description)
    return widget


def reveal(path: str) -> None:
    subprocess.Popen(["open", "-R", path])


def quick_look(path: str) -> None:
    """Open macOS Quick Look without ever blocking the GUI process."""
    subprocess.Popen(
        ["qlmanage", "-p", path], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )


# ---- воркери й помічники рескану: винесено у workers.py (2.17 «Solid Core») ----
from dupscan.ui.workers import (  # noqa: E402 — міст модулів
    Bg, DirectoryTrashWorker, MergePreparationWorker,
    MergeTransferWorker, PairVerificationWorker, PerceptualScanWorker,
    RecomputeWorker, ScanWorker, SessionLoadWorker, SessionRefreshWorker,
    containing_session_root, rebase_session_path,
)
from dupscan.ui.workers import (  # noqa: E402, F401 — реекспорт для тестів
    _dir_dates_for, _iter_session_refresh_cache_rows,
    _session_refresh_stats, build_session_refresh_cache_rows,
)


def _path_probe_score(key: str) -> int:
    """Deterministic, well-distributed score for picking rescan probes.

    Hashes the PATH STRING to rank candidates for sampling — unrelated to
    core._hash_file's BLAKE3 hash of FILE CONTENT, which detects duplicates.
    blake2b is used here only as a fast, stable string hash; the digest is
    never compared across processes or persisted.
    """
    return int.from_bytes(
        hashlib.blake2b(
            key.encode("utf-8", "surrogatepass"),
            digest_size=8,
            person=b"DupScanProbe",
        ).digest(),
        "big",
    )


def session_path_samples(
        previous: core.ScanResult,
        session_roots: tuple[str, ...] | list[str],
        per_root: int = 8) -> dict[str, tuple[str, ...]]:
    """Pick bounded, deterministic and representative probes per root.

    Lexicographic prefixes are unsafe here: real disks commonly begin with
    ``$RECYCLE.BIN`` and the root itself, which can exist even after the actual
    data tree moved below a new directory.  A stable path hash spreads probes
    across the snapshot while retaining only a tiny bounded heap.
    """
    roots = tuple(dict.fromkeys(
        os.path.normpath(os.path.abspath(root)) for root in session_roots
    ))
    limit = max(1, int(per_root))
    picked: dict[str, list[tuple[int, str, str]]] = {
        root: [] for root in roots
    }
    seen: dict[str, set[str]] = {root: set() for root in roots}

    def collect(source, *, blocked_roots: set[str] | None = None) -> None:
        blocked_roots = blocked_roots or set()
        for raw_path in source:
            path = os.path.normpath(os.path.abspath(raw_path))
            root = containing_session_root(path, roots)
            if root is None:
                continue
            if root in blocked_roots:
                continue
            if path == root:
                continue
            key = os.path.normcase(path)
            if key in seen[root]:
                continue
            seen[root].add(key)
            score = _path_probe_score(key)
            # Min-heap of negative scores: index 0 is the worst (largest)
            # retained score and is replaced by a smaller stable score.
            item = (-score, key, path)
            bucket = picked[root]
            if len(bucket) < limit:
                heapq.heappush(bucket, item)
            elif item > bucket[0]:
                heapq.heapreplace(bucket, item)

    collect(previous.file_meta)
    roots_with_files = {
        root for root, paths in picked.items() if paths
    }
    collect(previous.dir_ok, blocked_roots=roots_with_files)
    collect(previous.dir_files, blocked_roots=roots_with_files)
    return {
        root: tuple(
            item[2] for item in sorted(paths, key=lambda item: (-item[0], item[1]))
        )
        for root, paths in picked.items()
    }


def detect_nested_session_root(
        old_root: str, samples: tuple[str, ...] | list[str], *,
        probe_specs: dict[str, tuple[str, int | None]] | None = None,
        max_children: int = 128,
        cancel: threading.Event | None = None,
        ) -> tuple[str, int, int] | None:
    """Return one uniquely evidenced direct-child root, or fail closed.

    The caller runs this bounded filesystem probe off the GUI thread.  No
    recursive walk occurs: at most ``max_children`` direct entries and
    ``len(samples)`` translated paths per real non-symlink directory are
    inspected.
    """
    root = os.path.normpath(os.path.abspath(old_root))
    probes = tuple(dict.fromkeys(
        os.path.normpath(os.path.abspath(path)) for path in samples
    ))
    if (
        not probes
        or (cancel is not None and cancel.is_set())
        or not _real_directory(root)
    ):
        return None
    try:
        limit = max(1, min(512, int(max_children)))
    except (TypeError, ValueError):
        return None
    candidates: list[str] = []
    entries_seen = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if cancel is not None and cancel.is_set():
                    return None
                entries_seen += 1
                if entries_seen > limit:
                    return None
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
                candidates.append(
                    os.path.normpath(os.path.abspath(entry.path)))
    except OSError:
        return None

    normalized_specs = {
        os.path.normcase(os.path.normpath(os.path.abspath(path))): spec
        for path, spec in (probe_specs or {}).items()
    }
    scores: list[tuple[int, str]] = []
    for candidate in sorted(candidates, key=os.path.normcase):
        if cancel is not None and cancel.is_set():
            return None
        try:
            translated = tuple(
                rebase_session_path(path, root, candidate) for path in probes
            )
        except ValueError:
            return None
        hits = 0
        for old_path, current_path in zip(probes, translated, strict=True):
            if cancel is not None and cancel.is_set():
                return None
            spec = (
                normalized_specs.get(
                    os.path.normcase(old_path), ("unknown", None))
                if probe_specs is not None else None
            )
            if _session_probe_matches(current_path, spec):
                hits += 1
        scores.append((hits, candidate))
    if not scores:
        return None
    scores.sort(key=lambda item: (-item[0], os.path.normcase(item[1])))
    best_hits, best_path = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0
    if len(probes) == 1:
        proven = best_hits == 1 and runner_up == 0
    elif runner_up == 0:
        proven = best_hits >= 2
    else:
        coverage_floor = (3 * len(probes) + 3) // 4
        proven = (
            best_hits >= coverage_floor
            and best_hits - runner_up >= 2
        )
    if not proven:
        return None
    return best_path, best_hits, len(probes)


def _real_directory(path: str) -> bool:
    """True only for a directory entry itself, never a followed symlink."""
    try:
        return stat_mod.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _session_probe_matches(
        path: str, spec: tuple[str, int | None] | None) -> bool:
    """Validate a historical sample without following symlinks."""
    try:
        current = os.lstat(path)
    except OSError:
        return False
    if spec is None:
        return (
            stat_mod.S_ISREG(current.st_mode)
            or stat_mod.S_ISDIR(current.st_mode)
        )
    kind, expected_size = spec
    if kind == "file":
        return (
            stat_mod.S_ISREG(current.st_mode)
            and expected_size is not None
            and current.st_size == expected_size
        )
    if kind == "dir":
        return stat_mod.S_ISDIR(current.st_mode)
    return False


def session_probe_specs(
        previous: core.ScanResult,
        samples: tuple[str, ...] | list[str],
        ) -> dict[str, tuple[str, int | None]]:
    """Return type/size facts already stored in the historical snapshot."""
    specs: dict[str, tuple[str, int | None]] = {}
    unresolved: list[str] = []
    for raw_path in samples:
        path = os.path.normpath(os.path.abspath(raw_path))
        info = previous.file_meta.get(path)
        if info is not None:
            specs[path] = ("file", info.size)
        else:
            unresolved.append(path)
    if unresolved:
        normalized_directories = {
            os.path.normcase(os.path.normpath(os.path.abspath(path)))
            for path in set(previous.dir_ok) | set(previous.dir_files)
        }
        for path in unresolved:
            specs[path] = (
                ("dir", None)
                if os.path.normcase(path) in normalized_directories
                else ("unknown", None)
            )
    return specs


# ---- файлові операції: винесено у fsops.py (2.17 «Solid Core») -------------
# Реекспорти зберігають публічні імена модуля app для Main, воркерів і
# тестів. Обгортки нижче читають to_trash/_verified_survivor із глобалів
# app при КОЖНОМУ виклику, тому підміна app.to_trash (тести, інтеграції)
# перехоплює і внутрішні виклики fsops-оркестраторів.
from dupscan.infra.fsops import (  # noqa: E402 — свідомий міст після Qt-імпортів
    _failed_trash_paths, _trash_identity, _verified_survivor, to_trash,
)
from dupscan.infra.fsops import (  # noqa: E402, F401 — чисті реекспорти: тести кличуть
    # app._move_files/_copy_files напряму, а воркери читають app._same_device
    # пізнім lookup-ом (_app_hooks), тож імена мусять жити в неймспейсі app
    _TRASH_CONTEXT, TRASH_PARALLEL_THRESHOLD, TRASH_WORKERS,
    _class_digest, _contained, _copy_files, _directory_identity,
    _directory_identity_from_stat, _move_files, _open_directory_root,
    _operation_wait, _paranoid_verification, _proven_stat,
    _safe_destination_parent, _safe_source_parent, _same_device,
    _transfer_files,
)


def _to_trash_known(res, paths, *, expected_identities=None):
    return fsops._to_trash_known(
        res, paths, expected_identities=expected_identities, trash=to_trash)


def _verified_to_trash_files(res, paths: list[str]) -> list[str]:
    return fsops._verified_to_trash_files(
        res, paths, trash=to_trash, survivor=_verified_survivor)


def _verify_then_trash_dir(
    res, src_dir: str,
    cancel=None,
    pause=None,
    progress=None,
    extra_survivors: dict[str, str] | None = None,
    unproven_out: list[str] | None = None,
):
    return fsops._verify_then_trash_dir(
        res, src_dir, cancel=cancel, pause=pause, progress=progress,
        trash=to_trash, survivor=_verified_survivor,
        extra_survivors=extra_survivors, unproven_out=unproven_out)


class ElidingLabel(QLabel):
    """Однорядковий лейбл: довгий текст скорочується посередині.

    Геометрія НІКОЛИ не залежить від довжини тексту — саме перенос слів у
    зростаючому лейблі змушував вікно смикатися на кожному оновленні
    прогресу (шлях файла міняє довжину щотіку).
    """

    def __init__(self, text: str = "", parent=None):
        super().__init__("", parent)
        self._full_text = text
        self.setWordWrap(False)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        line = self.fontMetrics().height()
        self.setMinimumHeight(line + 6)
        self.setMaximumHeight(line + 6)
        self._apply()

    def setText(self, text: str) -> None:  # noqa: N802 — Qt API
        self._full_text = text or ""
        self._apply()

    def text(self) -> str:
        """ПОВНИЙ текст: логіка і тести бачать зміст, не візуальне скорочення."""
        return self._full_text

    def displayedText(self) -> str:  # noqa: N802 — Qt-стиль сусідніх методів
        """Те, що реально намальовано (скорочене посередині)."""
        return super().text()

    def _apply(self) -> None:
        width = max(48, self.width() - 8)
        super().setText(self.fontMetrics().elidedText(
            self._full_text, Qt.ElideMiddle, width))
        self.setToolTip(self._full_text)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt API
        super().resizeEvent(event)
        self._apply()


class ProgressLimiter:
    """ЄДИНИЙ обмежувач частоти оновлень прогресу.

    Раніше гейт обходився щоразу, коли змінювалася фаза, а фаза містила шлях
    поточного файла — тобто на кожному файлі. Тепер швидкість оновлень не
    залежить від кількості файлів.
    """

    def __init__(self, sink, interval: float = 0.1):
        self._sink = sink
        self._interval = interval
        self._last = 0.0

    def __call__(self, phase: str, done: int, total: int, force: bool = False) -> None:
        now = time.monotonic()
        if force or done == total or now - self._last >= self._interval:
            self._last = now
            self._sink(phase, done, total)


def _mib_phase_label(phase: str) -> str | None:
    """Якщо *phase* позначена суфіксом «· МіБ» (домовленість core.scan /
    workers.py / fsops.py для довгих байтових операцій — повне хешування
    при скані, перевірка перед злиттям, знімок теки перед Кошиком) —
    повертає короткий підпис етапу (частина рядка до першого «· »).
    Інакше — None (число прогресу тоді не байти, а штуки: файли, групи…).

    Середина рядка в цих фазах часто несе ПОТОЧНИЙ ШЛЯХ файла (наприклад
    «Перевіряю перед злиттям · /a/b · МіБ») — він міняється щофайл і
    навмисно ігнорується тут: підпис бере лише перший сегмент.
    """
    parts = phase.split(" · ")
    if len(parts) >= 2 and parts[-1] == "МіБ":
        return parts[0]
    return None


def _format_remaining(seconds: float) -> str:
    """UA-текст залишку часу. Завжди з «~» — це оцінка, не точний час."""
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"~{max(1, round(seconds))} с"
    minutes = seconds / 60
    if minutes < 60:
        return f"~{max(1, round(minutes))} хв"
    return f"~{minutes / 60:.1f} год"


class ProgressEta:
    """Оцінка «скільки лишилось» з наявного потоку progress(phase, done,
    total) — БЕЗ жодної зміни воркерів і БЕЗ диска в GUI-потоці: лише
    арифметика над числами, які й так приходять у on_progress. Час —
    виключно time.monotonic (ін'єктується для тестів; ніякого random чи
    datetime.now — детермінованість потрібна для тестів прогресу).

    «Потік» (stream) — це один етап однієї операції: своя стеля (*total*)
    і свій масштаб одиниць (байти-в-МіБ або штуки). Перемикання виявляється
    по (mib-ознака, total), а НЕ по точному тексту фази — байтові фази
    (див. _mib_phase_label) несуть у собі поточний шлях файла і тому
    змінюються щотіку, лишаючись тим самим потоком. done, що зменшився,
    теж означає новий потік (захист від випадкового збігу total).
    """

    _MIB = 1024 * 1024
    _WINDOW = 8       # ковзне вікно вимірів -> згладжена швидкість
    _MIN_SAMPLES = 3  # менше вимірів у поточному потоці -> оцінка ще хована
    _MIN_ELAPSED = 1.5  # секунд від початку потоку -> раніше оцінка хована

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        """Забути поточний потік — наступний update() почне новий відлік."""
        self._mib = False
        self._total = -1  # недосяжне для реального total (>=0): перший тік завжди новий потік
        self._start: float | None = None
        self._samples: deque[tuple[float, int]] = deque(maxlen=self._WINDOW)

    def update(self, phase: str, done: int, total: int) -> float | None:
        """Один тік. Повертає оцінку залишку в секундах, або None, якщо
        оцінки ще/більше немає (замало вимірів, total невідомий, готово)."""
        now = self._clock()
        mib = _mib_phase_label(phase) is not None
        last_done = self._samples[-1][1] if self._samples else None
        same_stream = (
            mib == self._mib and total == self._total
            and (last_done is None or done >= last_done)
        )
        if not same_stream:
            self._mib = mib
            self._total = total
            self._start = now
            self._samples.clear()
            last_done = None
        if last_done is None or done != last_done:
            self._samples.append((now, done))
        return self._estimate(now, total)

    def _estimate(self, now: float, total: int) -> float | None:
        if total <= 0 or len(self._samples) < self._MIN_SAMPLES:
            return None
        if self._start is None or now - self._start < self._MIN_ELAPSED:
            return None
        t0, d0 = self._samples[0]
        t1, d1 = self._samples[-1]
        if d1 >= total:
            return None  # завершення -> чистий рядок, без «лишилось»
        span = t1 - t0
        if span <= 0 or d1 <= d0:
            return None
        rate = (d1 - d0) / span
        if rate <= 0:
            return None
        return (total - d1) / rate

    def render(self, phase: str, done: int, total: int) -> str:
        """Повний рядок статусу: наявний формат «фаза: done/total» (або
        байтовий «підпис: X з Y ГБ» для МіБ-фаз), плюс «· лишилось ~Z»,
        коли оцінка вже певна."""
        remaining = self.update(phase, done, total)
        label = _mib_phase_label(phase)
        if total > 0 and label:
            base = (f"{label}: {human(done * self._MIB)} з "
                     f"{human(total * self._MIB)}")
        elif total > 0:
            base = f"{phase}: {done} / {total}"
        elif label:
            base = f"{label}: {human(done * self._MIB)}"
        else:
            base = f"{phase}: {done}" if done else phase
        if remaining is None:
            return base
        return f"{base} · лишилось {_format_remaining(remaining)}"


# ------------------------------------------------------------ список тек ----
class FolderList(QListWidget):
    """Теки скану: чекбокс = брати участь; ПКМ = прибрати; приймає drag&drop."""

    pathsDropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setMaximumHeight(96)
        self.setToolTip("Перетягніть теки з Finder або натисніть "
                        "«Додати джерела…».\n"
                        "Чекбокс вимикає теку без видалення зі списку.")

    def add_dir(self, path: str) -> None:
        path = os.path.abspath(path)
        for i in range(self.count()):
            if self.item(i).text() == path:
                return
        it = QListWidgetItem(path)
        it.setToolTip(path)
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked)
        self.addItem(it)

    def checked_dirs(self) -> list[str]:
        return [self.item(i).text() for i in range(self.count())
                if self.item(i).checkState() == Qt.Checked]

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [url.toLocalFile() for url in e.mimeData().urls()
                 if url.toLocalFile()]
        if paths:
            self.pathsDropped.emit(paths)
        e.acceptProposedAction()

    def contextMenuEvent(self, e):
        it = self.itemAt(e.pos())
        if it is None:
            return
        m = QMenu(self)
        act_rm = m.addAction("Прибрати зі списку")
        act_show = m.addAction("Показати у Finder")
        chosen = m.exec(e.globalPos())
        if chosen == act_rm:
            self.takeItem(self.row(it))
        elif chosen == act_show:
            reveal(it.text())


def directory_paths_from_selection(model, indexes) -> list[str]:
    """Return unique directory paths in selection order without disk probes."""
    paths: list[str] = []
    for index in indexes:
        if not model.isDir(index):
            continue
        path = os.path.normpath(os.path.abspath(model.filePath(index)))
        if path not in paths:
            paths.append(path)
    return paths


class SourceTreeProxy(QSortFilterProxyModel):
    """Expose only the user-data and volume branches at filesystem root."""

    def __init__(self, parent=None):
        super().__init__(parent)
        home = os.path.normpath(os.path.abspath(os.path.expanduser("~")))
        parts = home.split(os.sep)
        home_root = os.path.join(os.sep, parts[1]) if len(parts) > 1 else home
        self.allowed_root_paths = {
            os.path.normpath(home_root),
            os.path.normpath("/Volumes"),
        }

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        if model is None:
            return False
        # sourceModel() типізовано як базовий QAbstractItemModel
        # у стабах PySide6; ця proxy-модель за побудовою завжди фільтрує саме
        # QFileSystemModel (див. filesystem_model нижче) — cast не міняє
        # поведінку, лише називає вже гарантований інваріант.
        model = cast(QFileSystemModel, model)
        parent_path = os.path.normpath(model.filePath(source_parent))
        index = model.index(source_row, 0, source_parent)
        if parent_path != os.sep:
            return not model.fileName(index).startswith(".")
        return os.path.normpath(model.filePath(index)) in self.allowed_root_paths

    def filePath(self, index) -> str:
        return cast(QFileSystemModel, self.sourceModel()).filePath(self.mapToSource(index))

    def isDir(self, index) -> bool:
        return cast(QFileSystemModel, self.sourceModel()).isDir(self.mapToSource(index))

    def rootPath(self) -> str:
        return cast(QFileSystemModel, self.sourceModel()).rootPath()


class SourcePickerDialog(QDialog):
    """One filesystem tree for regular folders and mounted volume roots."""

    def __init__(self, parent=None, *,
                 title: str = "Додати джерела сканування",
                 accept_label: str = "Додати вибране"):
        super().__init__(parent)
        self.setObjectName("sourcePickerDialog")
        self.setWindowTitle(title)
        self.resize(780, 610)
        self._selection_order: list[str] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Оберіть будь-які папки та диски в одному дереві. "
            "Розкрийте «Users» для домашніх папок або «Volumes» для "
            "зовнішніх і мережевих томів; "
            "⌘-клік додає кілька джерел.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.filesystem_model = QFileSystemModel(self)
        self.filesystem_model.directoryLoaded.connect(self._directory_loaded)
        self.filesystem_model.setFilter(
            QDir.Filter.AllDirs
            | QDir.Filter.NoDotAndDotDot
            | QDir.Filter.Hidden
            | QDir.Filter.System)
        self.filesystem_model.setResolveSymlinks(False)
        source_root = self.filesystem_model.setRootPath("/")
        self.model = SourceTreeProxy(self)
        self.model.setSourceModel(self.filesystem_model)
        root_index = self.model.mapFromSource(source_root)

        self.tree = QTreeView()
        self.tree.setObjectName("sourceFilesystemTree")
        self.tree.setAccessibleName("Папки й диски для сканування")
        self.tree.setAccessibleDescription(
            "Єдине файлове дерево; можна одночасно вибрати папки й томи.")
        self.tree.setModel(self.model)
        self.tree.setRootIndex(root_index)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAnimated(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        for column in range(1, self.model.columnCount()):
            self.tree.setColumnHidden(column, True)
        layout.addWidget(self.tree, 1)

        self.selection_summary = QLabel("Завантажую папки й диски…")
        self.selection_summary.setObjectName("sourceSelectionSummary")
        self.selection_summary.setAccessibleName("Повні шляхи вибраних джерел")
        self.selection_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.selection_summary.setWordWrap(True)
        layout.addWidget(self.selection_summary)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = buttons.addButton(
            accept_label, QDialogButtonBox.ButtonRole.AcceptRole)
        self.accept_button.setObjectName("acceptSourcesButton")
        self.accept_button.setEnabled(False)
        cancel_button = buttons.button(
            QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setText("Скасувати")
        self.accept_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.tree.selectionModel().selectionChanged.connect(
            self._selection_changed)

    def _directory_loaded(self, path: str) -> None:
        if path == "/" and not self._selection_order:
            self.selection_summary.setText("Нічого не вибрано.")

    def _selection_changed(self, _selected=None, _deselected=None) -> None:
        current = directory_paths_from_selection(
            self.model, self.tree.selectionModel().selectedRows(0))
        selected = set(current)
        self._selection_order = [
            path for path in self._selection_order if path in selected]
        self._selection_order.extend(
            path for path in current if path not in self._selection_order)
        count = len(self._selection_order)
        self.accept_button.setEnabled(bool(count))
        if not count:
            self.selection_summary.setText("Нічого не вибрано.")
            return
        shown = self._selection_order[:4]
        text = "\n".join(shown)
        if count > len(shown):
            text += f"\n… і ще {count - len(shown)}"
        self.selection_summary.setText(f"Обрано: {count}\n{text}")

    def selected_paths(self) -> list[str]:
        return list(self._selection_order)


def pick_dirs(parent, *, title: str = "Додати джерела сканування",
              accept_label: str = "Додати вибране") -> list[str]:
    dialog = SourcePickerDialog(
        parent, title=title, accept_label=accept_label)
    return dialog.selected_paths() if dialog.exec() == QDialog.Accepted else []


class ResultTreeView(QTreeView):
    """Keyboard-efficient result view without QModelIndex-based batch state."""

    toggleCurrentRequested = Signal()
    focusSearchRequested = Signal()
    clearSearchRequested = Signal()

    def keyPressEvent(self, event) -> None:
        if event.modifiers() == Qt.NoModifier and event.key() == Qt.Key_Space:
            self.toggleCurrentRequested.emit()
            event.accept()
            return
        if event.modifiers() == Qt.NoModifier and event.key() == Qt.Key_Slash:
            self.focusSearchRequested.emit()
            event.accept()
            return
        if event.modifiers() == Qt.NoModifier and event.key() == Qt.Key_Escape:
            self.clearSearchRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SearchLineEdit(QLineEdit):
    """Search field whose Escape semantics never broaden a destructive scope."""

    escapePressed = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.escapePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# ---- моделі результатів ----
from dupscan.ui.table_models import (  # noqa: E402 — міст модулів
    ClusterModel, GroupModel, PerceptualModel, ProblemsModel, SimModel,
)
from dupscan.ui.table_models import (  # noqa: E402, F401 — реекспорт для тестів
    COLS, PAGE_SIZE, ROOT_PAGE_SIZE,
)

# view.model() у PySide6-стабах повертає базовий
# QAbstractItemModel, але дерева результатів у DupScan завжди дають
# GroupModel або SimModel — обидва мають той самий спільний набір
# custom-методів (root_id/root_row/total_children/item_key/index_for_key).
# Псевдонім лише називає вже наявний інваріант, поведінки не міняє.
_TreeModel = GroupModel | SimModel


class ReviewModel(QAbstractTableModel):
    """Flat, lazy review list that stays responsive for large selections."""

    HEADERS = ("До Кошика", "Категорія", "Назва", "Розташування",
               "Розмір", "Змінено", "Чому це дублікат")
    checksChanged = Signal()
    warning = Signal(str)

    def __init__(self, entries: list[dict], selected: set[str], groups: list[list[str]]):
        super().__init__()
        self.entries = entries
        self.selected = set(selected)
        self._visible = list(range(len(entries)))
        self._category = "all"
        self._groups: dict[str, frozenset[str]] = {}
        for group in groups:
            frozen = frozenset(group)
            for path in group:
                self._groups[path] = frozen

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.HEADERS[section]
        return None

    def entry_at(self, row: int) -> dict | None:
        if 0 <= row < len(self._visible):
            return self.entries[self._visible[row]]
        return None

    def flags(self, index):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        return base | Qt.ItemIsUserCheckable if index.column() == 0 else base

    def data(self, index, role=Qt.DisplayRole):
        entry = self.entry_at(index.row())
        if entry is None:
            return None
        path = entry["path"]
        if role == Qt.CheckStateRole and index.column() == 0:
            return Qt.Checked if path in self.selected else Qt.Unchecked
        if role == Qt.ToolTipRole:
            return path
        if role != Qt.DisplayRole:
            return None
        values = (
            "", product.category_label(entry["category"]), os.path.basename(path),
            os.path.dirname(path), human(entry["size"]),
            when(entry["mtime_ns"]) if entry["mtime_ns"] else "—", entry["reason"],
        )
        return values[index.column()]

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole:
            return False
        entry = self.entry_at(index.row())
        if entry is None:
            return False
        path = entry["path"]
        wants_selected = value == Qt.Checked or value == 2
        is_selected = path in self.selected
        if wants_selected == is_selected:
            return True
        if wants_selected:
            group = self._groups.get(path)
            if group and all(other == path or other in self.selected for other in group):
                self.warning.emit(
                    "У кожній групі мусить лишитися щонайменше одна копія.")
                return False
            self.selected.add(path)
        else:
            self.selected.remove(path)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.checksChanged.emit()
        return True

    def set_category(self, category: str) -> None:
        category = category if category in product.CATEGORY_LABELS else "all"
        self.beginResetModel()
        self._category = category
        self._visible = [
            row for row, entry in enumerate(self.entries)
            if category == "all" or entry["category"] == category
        ]
        self.endResetModel()

    def select_visible(self, on: bool) -> None:
        if not on:
            self.selected.difference_update(
                self.entries[row]["path"] for row in self._visible)
        else:
            for row in self._visible:
                path = self.entries[row]["path"]
                group = self._groups.get(path)
                if not group or not all(
                        other == path or other in self.selected for other in group):
                    self.selected.add(path)
        if self._visible:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self._visible) - 1, 0),
                                  [Qt.CheckStateRole])
        self.checksChanged.emit()


class ReviewDialog(QDialog):
    """Final human review before expensive verification/destructive actions."""

    def __init__(self, parent, entries: list[dict], selected: set[str],
                 groups: list[list[str]], *, action_text: str = "Продовжити перевірку"):
        super().__init__(parent)
        _accessible(
            self, "reviewDialog", "Перевірка вибраного",
            "Остаточний перегляд кандидатів перед перевіркою та Кошиком.")
        self.setWindowTitle("Перевірка вибраного — DupScan")
        self.resize(1080, 650)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Перегляньте кожен кандидат. Зняття позначки негайно змінює "
            "підсумок; позначити всі копії однієї групи неможливо.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Категорія:"))
        self.category = QComboBox()
        _accessible(self.category, "reviewCategory", "Категорія кандидатів")
        self.category.addItem(product.CATEGORY_LABELS["all"], "all")
        present = {entry["category"] for entry in entries}
        for key, label in product.CATEGORY_LABELS.items():
            if key != "all" and key in present:
                self.category.addItem(label, key)
        bar.addWidget(self.category)
        b_all = QPushButton("Позначити видимі")
        b_none = QPushButton("Зняти видимі")
        bar.addWidget(b_all)
        bar.addWidget(b_none)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.model = ReviewModel(entries, selected, groups)
        self.table = QTableView()
        _accessible(
            self.table, "reviewTable", "Кандидати до Кошика",
            "Галочка визначає кандидатів; повний шлях доступний у підказці.")
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 74)
        self.table.setColumnWidth(1, 125)
        self.table.setColumnWidth(2, 190)
        self.table.setColumnWidth(3, 280)
        self.table.setColumnWidth(4, 90)
        layout.addWidget(self.table, 1)

        details_row = QHBoxLayout()
        self.details = QLabel("Оберіть рядок для детальної інформації.")
        _accessible(self.details, "reviewDetails", "Повний шлях і доказ дубліката")
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        details_row.addWidget(self.details, 1)
        self.b_quick = _accessible(
            QPushButton("Quick Look"), "reviewQuickLook", "Переглянути через Quick Look")
        self.b_reveal = _accessible(
            QPushButton("Показати у Finder"), "reviewReveal", "Показати у Finder")
        details_row.addWidget(self.b_quick)
        details_row.addWidget(self.b_reveal)
        layout.addLayout(details_row)

        self.summary = QLabel()
        _accessible(self.summary, "reviewSummary", "Підсумок вибраних кандидатів")
        layout.addWidget(self.summary)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(action_text)
        layout.addWidget(buttons)

        self.category.currentIndexChanged.connect(
            lambda _i: self.model.set_category(self.category.currentData()))
        b_all.clicked.connect(lambda: self.model.select_visible(True))
        b_none.clicked.connect(lambda: self.model.select_visible(False))
        self.model.checksChanged.connect(self._update_summary)
        self.model.warning.connect(lambda text: QMessageBox.information(self, "DupScan", text))
        self.table.selectionModel().currentRowChanged.connect(
            lambda current, _old: self._show_entry(current.row()))
        self.b_quick.clicked.connect(lambda: self._with_current(quick_look))
        self.b_reveal.clicked.connect(lambda: self._with_current(reveal))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._update_summary()

    @property
    def selected_paths(self) -> set[str]:
        return set(self.model.selected)

    def _current_entry(self) -> dict | None:
        return self.model.entry_at(self.table.currentIndex().row())

    def _with_current(self, fn) -> None:
        entry = self._current_entry()
        if entry:
            fn(entry["path"])

    def _show_entry(self, row: int) -> None:
        entry = self.model.entry_at(row)
        if entry:
            self.details.setText(
                f"{entry['path']}\n{entry['reason']}\n"
                f"Розмір: {human(entry['size'])}")

    def _update_summary(self) -> None:
        chosen = [entry for entry in self.model.entries
                  if entry["path"] in self.model.selected]
        candidates = [
            {
                "size": int(entry["size"]),
                "group_id": self.model._groups.get(
                    entry["path"], frozenset((entry["path"],))),
                "status": "ready",
                "source_kind": (
                    "external_or_network"
                    if entry["path"].startswith("/Volumes/") else "local"),
            }
            for entry in chosen
        ]
        dry_run = scale_ui.summarize_candidates(candidates)
        self.summary.setText(
            f"До свіжої перевірки: {dry_run.ready_count:,} · "
            f"груп: {dry_run.ready_groups:,} · "
            f"обсяг: {human(dry_run.ready_bytes)} · "
            f"не позначено: {len(self.model.entries) - len(chosen):,}")


class FolderCompareModel(QAbstractTableModel):
    HEADERS = ("Стан", "Відносний шлях", "Тека A", "Тека B", "Розмір")
    STATUS_LABELS = {
        "identical": "Однаковий шлях і вміст",
        "shared_elsewhere": "Спільний вміст, інший шлях",
        "different_content": "Одна назва, різний вміст",
        "only_a": "Лише у A",
        "only_b": "Лише у B",
    }

    def __init__(self, comparison: product.FolderComparison):
        super().__init__()
        self.comparison = comparison
        self._status = "all"
        self._visible = list(range(len(comparison.rows)))

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._visible)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        return (self.HEADERS[section]
                if orientation == Qt.Horizontal and role == Qt.DisplayRole else None)

    def row_at(self, row: int):
        return (self.comparison.rows[self._visible[row]]
                if 0 <= row < len(self._visible) else None)

    def data(self, index, role=Qt.DisplayRole):
        row = self.row_at(index.row())
        if row is None:
            return None
        if role == Qt.ToolTipRole:
            if index.column() == 2:
                return row.path_a
            if index.column() == 3:
                return row.path_b
            return "\n".join(path for path in (row.path_a, row.path_b) if path)
        if role != Qt.DisplayRole:
            return None
        values = (self.STATUS_LABELS[row.status], row.relative_path,
                  row.path_a or "—", row.path_b or "—", human(row.bytes))
        return values[index.column()]

    def set_status(self, status: str) -> None:
        self.beginResetModel()
        self._status = status
        self._visible = [i for i, row in enumerate(self.comparison.rows)
                         if status == "all" or row.status == status]
        self.endResetModel()


class FolderCompareDialog(QDialog):
    def __init__(self, parent, comparison: product.FolderComparison, merge_callback=None,
                 merge_verification_required: bool = False):
        super().__init__(parent)
        _accessible(
            self, "folderCompareDialog", "Порівняння двох тек",
            "Порівняння повних шляхів теки A і теки B.")
        self.setWindowTitle("Порівняння двох тек — DupScan")
        self.resize(1120, 680)
        layout = QVBoxLayout(self)
        counts = comparison.counts
        self.summary = QLabel(
            f"A: {comparison.dir_a}\nB: {comparison.dir_b}\n"
            f"Однакових: {counts['identical']} · спільних в іншому місці: "
            f"{counts['shared_elsewhere']} · різного вмісту: "
            f"{counts['different_content']} · лише A/B: "
            f"{counts['only_a']}/{counts['only_b']}")
        _accessible(self.summary, "folderCompareSummary", "Підсумок тек A і B")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary)
        row = QHBoxLayout()
        row.addWidget(QLabel("Показати:"))
        self.filter_box = QComboBox()
        _accessible(self.filter_box, "folderCompareFilter", "Фільтр стану порівняння")
        self.filter_box.addItem("Усі", "all")
        for key, label in FolderCompareModel.STATUS_LABELS.items():
            self.filter_box.addItem(label, key)
        row.addWidget(self.filter_box)
        row.addStretch(1)
        layout.addLayout(row)
        self.model = FolderCompareModel(comparison)
        self.table = QTableView()
        _accessible(
            self.table, "folderCompareTable", "Результати порівняння тек",
            "Стовпці A і B зберігають повні шляхи у підказках.")
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 205)
        self.table.setColumnWidth(1, 235)
        self.table.setColumnWidth(2, 275)
        self.table.setColumnWidth(3, 275)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        self.b_quick = _accessible(
            QPushButton("Quick Look"), "folderCompareQuickLook",
            "Переглянути вибраний файл через Quick Look")
        self.b_reveal = _accessible(
            QPushButton("Показати у Finder"), "folderCompareReveal",
            "Показати вибраний файл у Finder")
        buttons.addWidget(self.b_quick)
        buttons.addWidget(self.b_reveal)
        if merge_callback is not None:
            prefix = "Перевірити й злити" if merge_verification_required else "Злити"
            b_a = QPushButton(f"{prefix} B → A…")
            b_b = QPushButton(f"{prefix} A → B…")
            _accessible(b_a, "mergeBToA", f"{prefix} з теки B до теки A")
            _accessible(b_b, "mergeAToB", f"{prefix} з теки A до теки B")
            b_a.clicked.connect(lambda: (self.accept(), merge_callback(comparison, True)))
            b_b.clicked.connect(lambda: (self.accept(), merge_callback(comparison, False)))
            buttons.addWidget(b_a)
            buttons.addWidget(b_b)
        buttons.addStretch(1)
        b_close = QPushButton("Закрити")
        buttons.addWidget(b_close)
        layout.addLayout(buttons)
        self.filter_box.currentIndexChanged.connect(
            lambda _i: self.model.set_status(self.filter_box.currentData()))
        self.b_quick.clicked.connect(lambda: self._open_current(quick_look))
        self.b_reveal.clicked.connect(lambda: self._open_current(reveal))
        b_close.clicked.connect(self.accept)

    def _open_current(self, fn) -> None:
        row = self.model.row_at(self.table.currentIndex().row())
        if row:
            fn(row.path_a or row.path_b)


class PreferencesDialog(QDialog):
    """Profiles and deterministic keeper rules in one native preferences UI."""

    def __init__(self, parent, profiles: list[preferences.ScanProfile],
                 current: preferences.ScanProfile, rules: preferences.SelectionRules):
        super().__init__(parent)
        self.setWindowTitle("Налаштування DupScan")
        self.resize(620, 690)
        self.saved_profile: preferences.ScanProfile | None = None
        self._profiles = profiles
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.profile_box = QComboBox()
        for profile in profiles:
            self.profile_box.addItem(profile.name, profile)
        self.name = QLineEdit()
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 2_000_000_000)
        self.min_size.setSuffix(" МБ")
        self.include_ext = QLineEdit()
        self.include_ext.setPlaceholderText("jpg, png, mov — порожньо означає всі")
        self.exclude_ext = QLineEdit()
        self.exclude_ext.setPlaceholderText("tmp, cache")
        self.exclude_paths = QPlainTextEdit()
        self.exclude_paths.setMaximumHeight(92)
        self.exclude_paths.setPlaceholderText("Один абсолютний шлях на рядок")
        self.hidden = QCheckBox("Сканувати приховані файли й теки")
        self.bundles = QCheckBox("Сканувати вміст пакетів (.app, .photoslibrary …)")
        self.symlinks = QCheckBox("Ураховувати символічні посилання")
        form.addRow("Профіль:", self.profile_box)
        form.addRow("Назва:", self.name)
        form.addRow("Мінімальний розмір:", self.min_size)
        form.addRow("Лише розширення:", self.include_ext)
        form.addRow("Не сканувати розширення:", self.exclude_ext)
        form.addRow("Виключені шляхи:", self.exclude_paths)
        form.addRow("", self.hidden)
        form.addRow("", self.bundles)
        form.addRow("", self.symlinks)
        layout.addLayout(form)

        layout.addWidget(QLabel("Розумний вибір копії, яку потрібно залишити"))
        rules_form = QFormLayout()
        self.keep = QComboBox()
        self.keep.addItem("Найновішу", "newest")
        self.keep.addItem("Найстарішу", "oldest")
        self.keep.addItem("Першу за шляхом", "lexical")
        self.always_keep = QPlainTextEdit()
        self.prefer_keep = QPlainTextEdit()
        self.prefer_remove = QPlainTextEdit()
        for edit in (self.always_keep, self.prefer_keep, self.prefer_remove):
            edit.setMaximumHeight(58)
            edit.setPlaceholderText("Один абсолютний шлях на рядок")
        self.internal = QCheckBox("За інших рівних умов лишати копію на внутрішньому диску")
        rules_form.addRow("Політика дати:", self.keep)
        rules_form.addRow("Завжди залишати:", self.always_keep)
        rules_form.addRow("Бажано залишати:", self.prefer_keep)
        rules_form.addRow("Бажано прибирати:", self.prefer_remove)
        rules_form.addRow("", self.internal)
        layout.addLayout(rules_form)

        bottom = QHBoxLayout()
        self.delete_button = QPushButton("Видалити профіль")
        bottom.addWidget(self.delete_button)
        bottom.addStretch(1)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Save).setText("Зберегти й застосувати")
        bottom.addWidget(box)
        layout.addLayout(bottom)

        self.profile_box.currentIndexChanged.connect(self._load_profile)
        self.delete_button.clicked.connect(self._delete_profile)
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        index = next((i for i, item in enumerate(profiles) if item == current), 0)
        self.profile_box.setCurrentIndex(index)
        self._load_profile(index)
        self.keep.setCurrentIndex(max(0, self.keep.findData(rules.keep)))
        self.always_keep.setPlainText("\n".join(rules.always_keep_paths))
        self.prefer_keep.setPlainText("\n".join(rules.prefer_keep_paths))
        self.prefer_remove.setPlainText("\n".join(rules.prefer_remove_paths))
        self.internal.setChecked(rules.prefer_keep_internal)

    @staticmethod
    def _lines(edit: QPlainTextEdit) -> tuple[str, ...]:
        return tuple(line.strip() for line in edit.toPlainText().splitlines()
                     if line.strip())

    @staticmethod
    def _extensions(edit: QLineEdit) -> tuple[str, ...]:
        return tuple(value.strip() for value in edit.text().replace(";", ",").split(",")
                     if value.strip())

    def _load_profile(self, index: int) -> None:
        profile = self.profile_box.itemData(index)
        if not isinstance(profile, preferences.ScanProfile):
            return
        self.name.setText(profile.name)
        self.min_size.setValue(profile.min_size // (1024 * 1024))
        self.include_ext.setText(", ".join(profile.include_extensions))
        self.exclude_ext.setText(", ".join(profile.excluded_extensions))
        self.exclude_paths.setPlainText("\n".join(profile.excluded_paths))
        self.hidden.setChecked(profile.include_hidden)
        self.bundles.setChecked(profile.include_bundles)
        self.symlinks.setChecked(profile.include_symlinks)
        self.delete_button.setEnabled(profile.name != preferences.DEFAULT_PROFILE_NAME)

    def _delete_profile(self) -> None:
        profile = self.profile_box.currentData()
        if not isinstance(profile, preferences.ScanProfile):
            return
        try:
            if preferences.delete_profile(profile.name):
                self.profile_box.removeItem(self.profile_box.currentIndex())
        except (ValueError, preferences.PreferencesError) as error:
            QMessageBox.warning(self, "DupScan", str(error))

    def _save(self) -> None:
        try:
            profile = preferences.ScanProfile(
                name=self.name.text(), min_size=self.min_size.value() * 1024 * 1024,
                include_extensions=self._extensions(self.include_ext),
                excluded_extensions=self._extensions(self.exclude_ext),
                excluded_paths=self._lines(self.exclude_paths),
                include_hidden=self.hidden.isChecked(),
                include_bundles=self.bundles.isChecked(),
                include_symlinks=self.symlinks.isChecked(),
            )
            rules = preferences.SelectionRules(
                keep=self.keep.currentData(),
                always_keep_paths=self._lines(self.always_keep),
                prefer_keep_paths=self._lines(self.prefer_keep),
                prefer_remove_paths=self._lines(self.prefer_remove),
                prefer_keep_internal=self.internal.isChecked(),
            )
            preferences.save_profile(profile)
            preferences.save_selection_rules(rules)
            self.saved_profile = profile
            self.accept()
        except (TypeError, ValueError, preferences.PreferencesError) as error:
            QMessageBox.warning(self, "Некоректні налаштування", str(error))


class ProblemsDialog(QDialog):
    PAGE_SIZE = 500

    def __init__(
        self, parent, errors: list[str], total_count: int | None = None
    ):
        super().__init__(parent)
        self.setWindowTitle("Центр проблем — DupScan")
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        grouped: dict[str, int] = {}
        for message in errors:
            title, _advice = product.classify_problem(message)
            grouped[title] = grouped.get(title, 0) + 1
        total_count = max(len(errors), total_count or 0)
        overview = " · ".join(f"{key}: {value}" for key, value in grouped.items())
        if total_count > len(errors):
            overview += (
                f" · показано деталей {len(errors):,} із {total_count:,}")
        layout.addWidget(QLabel(overview or "Проблем не виявлено."))
        self._errors = list(errors)
        self._loaded = 0
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.b_more = QPushButton()
        self.b_more.clicked.connect(self._load_more)
        layout.addWidget(self.b_more)
        self.details = QTextBrowser()
        self.details.setMaximumHeight(135)
        layout.addWidget(self.details)
        row = QHBoxLayout()
        b_reveal = QPushButton("Показати шлях у Finder")
        b_close = QPushButton("Закрити")
        row.addWidget(b_reveal)
        row.addStretch(1)
        row.addWidget(b_close)
        layout.addLayout(row)

        def current_changed(item):
            if item:
                message, advice = item.data(Qt.UserRole)
                self.details.setPlainText(f"{message}\n\nЩо зробити: {advice}")

        self.list.currentItemChanged.connect(lambda current, _old: current_changed(current))
        b_reveal.clicked.connect(self._reveal_current)
        b_close.clicked.connect(self.accept)
        self._load_more()
        if self.list.count():
            self.list.setCurrentRow(0)

    def _load_more(self) -> None:
        end = min(len(self._errors), self._loaded + self.PAGE_SIZE)
        for message in self._errors[self._loaded:end]:
            title, advice = product.classify_problem(message)
            item = QListWidgetItem(f"{title}: {message}")
            item.setData(Qt.UserRole, (message, advice))
            self.list.addItem(item)
        self._loaded = end
        remaining = len(self._errors) - self._loaded
        self.b_more.setText(
            f"Показати ще {min(self.PAGE_SIZE, remaining):,} "
            f"(лишилось {remaining:,})"
            if remaining else f"Показано всі {len(self._errors):,}"
        )
        self.b_more.setEnabled(remaining > 0)

    def _reveal_current(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        message, _advice = item.data(Qt.UserRole)
        candidates = [part for part in message.split(": ") if part.startswith("/")]
        if candidates:
            reveal(candidates[0])


def _review_bypass_allowed(modules: Container[str] | None = None) -> bool:
    """Чи дозволено пропустити ReviewDialog без відповіді людини.

    offscreen сам собою НЕ доказ автоматизації — цю змінну середовища
    можна виставити і перед запуском зібраного .app.
    Обхід вимагає ДРУГОЇ, незалежної ознаки: `"pytest" in sys.modules`,
    якої в зібраному застосунку немає за побудовою (PyInstaller не тягне
    pytest). `modules` — параметр для юніт-тесту самого helper-а без
    ризику мутувати реальний sys.modules під час прогону pytest.
    """
    mods = sys.modules if modules is None else modules
    return (
        os.environ.get("QT_QPA_PLATFORM") == "offscreen"
        and "pytest" in mods
    )


class _SimMergeOp:
    """Дані однієї операції злиття пари (вкладка «Схожість»).

    `Main._run_sim_merge` розбитий на приватні кроки-методи; замість
    вкладених замикань, що ділили локальні змінні, кроки ділять цей
    об'єкт — незмінні параметри операції (пара, напрям, знімок) і
    мінливий `ctx` (перенесені файли, воркер перевірки Кошика тощо)."""

    def __init__(self, *, pr, into_a: bool, res: core.ScanResult,
                 pair_verified: bool, src_dir: str, dst_dir: str,
                 source_side: str, target_side: str,
                 context_token, operation_result) -> None:
        self.pr = pr
        self.into_a = into_a
        self.res = res
        self.pair_verified = pair_verified
        self.src_dir = src_dir
        self.dst_dir = dst_dir
        self.source_side = source_side
        self.target_side = target_side
        self.context_token = context_token
        self.operation_result = operation_result
        self.ctx: dict = {"moved": []}


class _SessionLoadOp:
    """Параметри однієї операції завантаження сесії — спільний контекст
    для приватних кроків `Main._load_session_*`, той самий прийом, що й
    `_SimMergeOp` для злиття."""

    def __init__(self, *, path: str, roots: list[str],
                 refresh_after_load: bool, context_token) -> None:
        self.path = path
        self.roots = roots
        self.refresh_after_load = refresh_after_load
        self.context_token = context_token
        self.worker: SessionLoadWorker | None = None


# ------------------------------------------------------------------ вікно ----
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{DISPLAY_NAME} — пошук дублікатів")
        self.resize(1180, 760)
        self.worker: ScanWorker | None = None
        self.session_load_worker: SessionLoadWorker | None = None
        self.refresh_worker: SessionRefreshWorker | None = None
        self.pair_worker: PairVerificationWorker | None = None
        self.merge_worker: MergePreparationWorker | None = None
        # Бракувало декларації поруч з іншими воркерами вище
        # — _rw існував лише як динамічний getattr(self, "_rw", None) на
        # чотирьох сайтах перевірки, mypy не міг вивести тип.
        self._rw: RecomputeWorker | None = None
        self.result: core.ScanResult | None = None
        # Який self.result перцептивні групи стосуються — інший
        # об'єкт результату (нове сканування/завантажена сесія) робить їх
        # застарілими, _finish_model_refresh очищає.
        self._perceptual_scanned_for: core.ScanResult | None = None
        self._dir_dates: dict[str, tuple[int, int]] = {}
        self._last_roots: list[str] = []
        self._loaded_session_roots: tuple[str, ...] = ()
        self._loaded_session_path: str | None = None
        self._session_root_map: dict[str, str] = {}
        # Last user-confirmed mappings survive switching historical sessions,
        # but are only hints: every requested translated path is revalidated.
        self._session_relink_hints: dict[str, str] = {}
        self._session_relinking = False
        self._session_context_token = 0
        self._refresh_preflighting = False
        self._refresh_preflight_cancel: threading.Event | None = None
        self._storage_preflighting = False
        self._storage_preflight_token = 0
        self._refresh_models_deferred = False
        # A full-refresh root selected by the user is not the same thing as a
        # selective A/B relink.  Keep it separate so a pair cannot narrow a
        # disk refresh, while an explicit nested mount choice is not discarded.
        self._session_refresh_root_overrides: dict[str, str] = {}
        self._pending_result_note = ""
        self._progress: tuple[str, int, int] | None = None  # (фаза, зроблено, всього)
        self._eta = ProgressEta()  # ETA для статус-рядка
        self._bg: set[Bg] = set()  # живі фонові роботи (тримаємо від GC)
        self._ui_bg: set[Bg] = set()  # пошук/навігація; не блокує інші дії
        self._filter_cancel: dict[GroupModel, threading.Event] = {}
        self._filter_status: dict[GroupModel, str] = {}
        self._model_views: dict[object, QTreeView] = {}
        self._closing = False
        self._close_ready = False
        self._shutdown_started = 0.0
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(50)
        self._shutdown_timer.timeout.connect(self._poll_shutdown)
        self._tree_bulk_token: dict[QTreeView, int] = {}
        self._tree_bulk_status: dict[QTreeView, str] = {}
        self._sweeping = False
        self._last_sweep = 0.0
        self._loading = False
        self._trashing = False
        self._merging = False
        self._batch_selecting = False
        self._batch_selection_token = 0
        self._model_preparing = False
        self._model_prepare_token = 0
        self._model_prepare_cancel: threading.Event | None = None
        self._compare_after_scan: tuple[str, str] | None = None
        self._column_base_widths: dict[QTreeView, list[int]] = {}
        self._resizing_columns: set[QTreeView] = set()
        self._sort_view_state: dict[QTreeView, tuple[set[int], object, int]] = {}
        self._sort_previous_status: dict[QTreeView, str] = {}
        settings_base = os.environ.get("DUPSCAN_DATA_DIR") or os.path.expanduser(
            "~/Library/Application Support/DupScan")
        os.makedirs(settings_base, exist_ok=True)
        self._settings = QSettings(
            os.path.join(settings_base, "ui.ini"), QSettings.IniFormat)
        self._view_names: dict[QTreeView, str] = {}
        self._search_boxes: dict[object, QLineEdit] = {}
        self._category_boxes: dict[object, QComboBox] = {}
        self._preset_boxes: dict[object, QComboBox] = {}
        self._density_buttons: dict[object, QAction] = {}
        self._inspector_buttons: dict[object, QAction] = {}
        self._selection_menu_buttons: dict[object, QPushButton] = {}
        self._view_menu_buttons: dict[object, QPushButton] = {}
        self._inspectors: dict[object, QWidget] = {}
        self._splitters: dict[object, QSplitter] = {}
        self._inspector_last_size: dict[object, int] = {}
        self._similarity_tags: dict[tuple[str, str], str] = {}
        try:
            saved_tags = json.loads(str(self._settings.value("similarity/tags", "{}")))
            if isinstance(saved_tags, dict):
                for raw_key, tag in list(saved_tags.items())[:1000]:
                    parts = raw_key.split("\n") if isinstance(raw_key, str) else []
                    if (len(parts) == 2 and all(os.path.isabs(path) for path in parts)
                            and tag in {"До перевірки", "Схвалено", "Відкласти"}):
                        # Явна 2-кортежна деструктуризація —
                        # tuple(sorted(parts)) статично tuple[str, ...],
                        # хоча len(parts)==2 вище й гарантує довжину 2.
                        first, second = sorted(parts)
                        self._similarity_tags[(first, second)] = tag
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        self._preferences_error = ""
        try:
            self._profiles = preferences.list_profiles()
            wanted_profile = str(self._settings.value(
                "scan/current_profile", preferences.DEFAULT_PROFILE_NAME))
            try:
                self._profile = preferences.load_profile(wanted_profile)
            except KeyError:
                self._profile = preferences.DEFAULT_PROFILE
            self._selection_rules = preferences.load_selection_rules()
        except preferences.PreferencesError as error:
            self._profiles = [preferences.DEFAULT_PROFILE]
            self._profile = preferences.DEFAULT_PROFILE
            self._selection_rules = preferences.DEFAULT_SELECTION_RULES
            self._preferences_error = str(error)

        # get_status/get_profile_summary — пізнє зв'язування: ці віджети
        # створює _build_workflow_shell() нижче, а _refresh_profile_summary
        # (делегат на цей контролер) вона ж і кличе зсередини себе.
        self._settings_ctl = SettingsController(
            parent=self,
            get_status=lambda: self.status,
            get_profile_summary=lambda: self.profile_summary,
            settings=self._settings,
            get_profile=lambda: self._profile,
            set_profile=lambda value: setattr(self, "_profile", value),
            get_profiles=lambda: self._profiles,
            set_profiles=lambda value: setattr(self, "_profiles", value),
            set_selection_rules=lambda value: setattr(self, "_selection_rules", value),
            make_dialog=lambda *args, **kwargs: PreferencesDialog(*args, **kwargs),
        )

        self._build_workflow_shell()
        self._reports = ReportsController(
            parent=self,
            status=self.status,
            settings=self._settings,
            get_result=lambda: self.result,
            get_selection_paths=lambda: self._selection_paths(),
            get_last_roots=lambda: self._last_roots,
            ui_bg_run=lambda *args, **kwargs: self._ui_bg_run(*args, **kwargs),
            read_crash_log=lambda: _read_crash_log(),
        )
        self._removal_history_ctl = RemovalHistoryController(
            parent=self,
            get_last_roots=lambda: self._last_roots,
            ui_bg_run=lambda *args, **kwargs: self._ui_bg_run(*args, **kwargs),
        )
        self._folder_comparison_ctl = FolderComparisonController(
            parent=self,
            pick_dirs=lambda *args, **kwargs: pick_dirs(*args, **kwargs),
            get_operation_busy=lambda: self._operation_busy(),
            get_result=lambda: self.result,
            add_dir=lambda path: self.folders.add_dir(path),
            set_compare_after_scan=lambda value: setattr(
                self, "_compare_after_scan", value),
            begin_scan=lambda roots: self._begin_scan(roots),
            get_sim_merge=lambda: self._sim_merge,
            make_dialog=lambda *args, **kwargs: FolderCompareDialog(*args, **kwargs),
        )

        self._create_menus()
        self._apply_accessibility()
        if self._preferences_error:
            self.status.setText(
                "Налаштування пошкоджені; використано безпечні типові значення. "
                + self._preferences_error)
        QTimer.singleShot(250, self._show_onboarding_if_needed)

        # QApplication.instance() у PySide6-стабах повертає
        # QCoreApplication | None (базовий клас без applicationStateChanged,
        # без урахування підкласу виклику) — але Main конструюється лише
        # ПІСЛЯ створення QApplication, тож інстанс тут завжди є і завжди
        # саме QApplication.
        qapp = cast(QApplication, QApplication.instance())
        qapp.applicationStateChanged.connect(self._on_app_state)

    def _build_workflow_shell(self) -> None:
        # Models can emit sortStarted while result views restore persisted
        # sorting during construction. The shared status target must therefore
        # exist before any task page or view is built.
        self.status = ElidingLabel(
            "Додайте теки й почніть новий пошук. Файли обробляються локально.")
        self.status.setObjectName("globalStatus")

        root = QWidget()
        root.setObjectName("appRoot")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("taskSidebar")
        sidebar.setFixedWidth(220)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 18, 14, 14)
        side.setSpacing(6)
        brand = QLabel(f"DupScan {__version__}")
        brand.setObjectName("brandTitle")
        side.addWidget(brand)
        brand_hint = QLabel("Безпечне прибирання дублікатів")
        brand_hint.setObjectName("brandHint")
        brand_hint.setWordWrap(True)
        side.addWidget(brand_hint)
        side.addSpacing(16)

        self._task_group = QButtonGroup(self)
        self._task_group.setExclusive(True)

        def nav_button(text: str, object_name: str, accessible_name: str) -> QPushButton:
            button = _accessible(
                QPushButton(text), object_name, accessible_name)
            button.setCheckable(True)
            button.setProperty("nav", True)
            self._task_group.addButton(button)
            side.addWidget(button)
            return button

        self.nav_scan = nav_button(
            "Новий пошук", "newScanNavigationButton",
            "Відкрити новий пошук дублікатів")
        self.nav_results = nav_button(
            "Результати", "resultsNavigationButton",
            "Відкрити результати сканування")
        self.nav_compare = nav_button(
            "Порівняти A / B", "compareFoldersButton",
            "Порівняти теку A і теку B")
        self.nav_scan.clicked.connect(
            lambda _checked=False: self._show_task(workflow_ui.TASK_SCAN))
        self.nav_results.clicked.connect(
            lambda _checked=False: self._show_task(workflow_ui.TASK_RESULTS))
        self.nav_compare.clicked.connect(
            lambda _checked=False: self._show_task(workflow_ui.TASK_COMPARE))

        side.addStretch(1)
        utility_label = QLabel("СЛУЖБОВІ")
        utility_label.setObjectName("sidebarSectionLabel")
        side.addWidget(utility_label)
        b_history = _accessible(
            QPushButton("Історія сесій…"), "sessionHistoryButton",
            "Історія сканувань")
        b_history.setProperty("utility", True)
        b_history.clicked.connect(self.show_history)
        side.addWidget(b_history)
        b_remove_history = _accessible(
            QPushButton("Кошик і відновлення…"), "removalHistoryButton",
            "Історія переміщення до Кошика й відновлення")
        b_remove_history.setProperty("utility", True)
        b_remove_history.clicked.connect(self.show_removal_history)
        side.addWidget(b_remove_history)
        self.b_preferences = _accessible(
            QPushButton("Налаштування…"), "preferencesButton",
            "Налаштування сканування")
        self.b_preferences.setProperty("utility", True)
        self.b_preferences.clicked.connect(self.show_preferences)
        side.addWidget(self.b_preferences)
        version_label = QLabel(f"Варіант · {VARIANT_NAME}")
        version_label.setObjectName("versionLabel")
        side.addWidget(version_label)
        outer.addWidget(sidebar)

        main_host = QWidget()
        main_host.setObjectName("taskHost")
        main_layout = QVBoxLayout(main_host)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.task_stack = QStackedWidget()
        self.task_stack.setObjectName("taskStack")
        self.page_scan = self._build_scan_task()
        self.page_results = self._build_results_task()
        self.page_compare = self._build_compare_task()
        self._task_pages = {
            workflow_ui.TASK_SCAN: self.page_scan,
            workflow_ui.TASK_RESULTS: self.page_results,
            workflow_ui.TASK_COMPARE: self.page_compare,
        }
        for page in self._task_pages.values():
            self.task_stack.addWidget(page)
        main_layout.addWidget(self.task_stack, 1)

        status_frame = QFrame()
        status_frame.setObjectName("globalStatusBar")
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(22, 7, 22, 8)
        status_layout.addWidget(self.status, 1)
        self.operation_controls = QWidget()
        operation_layout = QHBoxLayout(self.operation_controls)
        operation_layout.setContentsMargins(0, 0, 0, 0)
        operation_layout.setSpacing(7)
        self.bar.setMinimumWidth(220)
        operation_layout.addWidget(self.bar)
        operation_layout.addWidget(self.b_pause)
        operation_layout.addWidget(self.b_cancel)
        self.operation_controls.setVisible(False)
        status_layout.addWidget(self.operation_controls)
        main_layout.addWidget(status_frame)
        outer.addWidget(main_host, 1)
        self.setCentralWidget(root)

        for model in (self.m_files, self.m_dirs, self.m_sim):
            model.warn = self.status.setText
            model.checksChanged.connect(self._update_selection_summary)
        self.folders.pathsDropped.connect(self._add_dirs_async)
        self._show_task(workflow_ui.TASK_SCAN)
        self.setStyleSheet("""
            QFrame#taskSidebar {
                background: palette(alternate-base);
                border-right: 1px solid palette(mid);
            }
            QLabel#brandTitle {
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#brandHint, QLabel#versionLabel, QLabel#sidebarSectionLabel {
                color: palette(placeholder-text);
            }
            QLabel#releaseBadge {
                color: palette(highlight);
                font-size: 11px;
                font-weight: 700;
                padding: 3px 7px;
                border: 1px solid palette(highlight);
                border-radius: 6px;
            }
            QLabel#sidebarSectionLabel {
                font-size: 10px;
                font-weight: 600;
            }
            QPushButton[nav="true"], QPushButton[utility="true"] {
                text-align: left;
                min-height: 30px;
                padding: 3px 9px;
                border: 0;
                border-radius: 7px;
                background: transparent;
            }
            QPushButton[nav="true"]:hover, QPushButton[utility="true"]:hover {
                background: palette(midlight);
            }
            QPushButton[nav="true"]:checked {
                background: palette(highlight);
                color: palette(highlighted-text);
                font-weight: 600;
            }
            QLabel[pageTitle="true"] {
                font-size: 24px;
                font-weight: 700;
            }
            QLabel[pageHint="true"] {
                color: palette(placeholder-text);
            }
            QLabel[sectionTitle="true"] {
                font-size: 15px;
                font-weight: 600;
            }
            QFrame[section="true"], QFrame#selectionActionBar {
                background: palette(base);
                border: 1px solid palette(mid);
                border-radius: 9px;
            }
            QFrame#resultQualityNotice {
                background: palette(alternate-base);
                border: 1px solid palette(mid);
                border-radius: 8px;
            }
            QFrame#sessionRescanPanel {
                background: palette(alternate-base);
                border: 1px solid palette(highlight);
                border-radius: 8px;
            }
            QLabel#sessionRescanText {
                font-weight: 600;
            }
            QFrame#resultsOperationBanner[active="false"] {
    background: transparent;
    border: 1px solid transparent;
}
QFrame#resultsOperationBanner {
                background: palette(highlight);
                border: 1px solid palette(highlight);
                border-radius: 8px;
            }
            QLabel#resultsOperationText {
                color: palette(highlighted-text);
                font-weight: 600;
            }
            QFrame#globalStatusBar {
                border-top: 1px solid palette(mid);
                background: palette(alternate-base);
            }
        """)

    def _page_heading(self, title: str, hint: str) -> tuple[QLabel, QLabel]:
        heading = QLabel(title)
        heading.setProperty("pageTitle", True)
        description = QLabel(hint)
        description.setProperty("pageHint", True)
        description.setWordWrap(True)
        return heading, description

    def _build_scan_task(self) -> QWidget:
        page = QWidget()
        page.setObjectName("scanTaskPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(12)
        heading, hint = self._page_heading(
            "Новий пошук",
            "Оберіть місця, які потрібно перевірити. DupScan читає й хешує "
            "файли локально та нічого не видаляє під час сканування.")
        layout.addWidget(heading)
        layout.addWidget(hint)

        source_section = QFrame()
        source_section.setProperty("section", True)
        source_layout = QVBoxLayout(source_section)
        source_layout.setContentsMargins(16, 14, 16, 16)
        self.sources_heading = QLabel("Джерела сканування")
        self.sources_heading.setProperty("sectionTitle", True)
        source_layout.addWidget(self.sources_heading)
        self.folders = FolderList()
        self.folders.setMaximumHeight(230)
        source_layout.addWidget(self.folders)
        folder_actions = QHBoxLayout()
        self.b_add_sources = _accessible(
            QPushButton("Додати джерела…"), "addSourcesButton",
            "Додати папки або диски")
        self.b_add_sources.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon))
        self.b_add_sources.clicked.connect(self.add_dirs)
        b_clear = _accessible(
            QPushButton("Очистити список"), "clearFoldersButton",
            "Очистити список тек")
        b_clear.clicked.connect(self.folders.clear)
        folder_actions.addWidget(self.b_add_sources)
        folder_actions.addWidget(b_clear)
        folder_actions.addStretch(1)
        self.profile_summary = QLabel()
        self.profile_summary.setObjectName("activeProfileSummary")
        self.profile_summary.setToolTip(
            "Змінити профіль можна через «Налаштування…» у бічній панелі.")
        self._refresh_profile_summary()
        folder_actions.addWidget(self.profile_summary)
        source_layout.addLayout(folder_actions)
        layout.addWidget(source_section)

        scan_controls = QHBoxLayout()
        self.b_scan = QPushButton("Почати сканування")
        self.b_scan.setDefault(True)
        self.b_scan.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.b_scan.clicked.connect(self.start_scan)
        self.b_pause = QPushButton("Пауза")
        self.b_pause.setEnabled(False)
        self.b_pause.clicked.connect(self.toggle_pause)
        self.b_cancel = QPushButton("Скасувати")
        self.b_cancel.setEnabled(False)
        self.b_cancel.clicked.connect(self.cancel_scan)
        scan_controls.addWidget(self.b_scan)
        scan_controls.addStretch(1)
        layout.addLayout(scan_controls)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        scan_note = QLabel(
            "Після завершення DupScan автоматично відкриє результати. "
            "Скасований пошук не дозволяє прибирання без нової перевірки.")
        scan_note.setProperty("pageHint", True)
        scan_note.setWordWrap(True)
        layout.addWidget(scan_note)
        layout.addStretch(1)
        return page

    def _build_results_task(self) -> QWidget:
        page = QWidget()
        page.setObjectName("resultsTaskPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 22, 22, 18)
        layout.setSpacing(9)
        header = QHBoxLayout()
        heading, hint = self._page_heading(
            "Результати",
            "Спочатку оберіть групу, потім перевірте повні шляхи й лише тоді "
            "позначайте зайві копії до Кошика.")
        header_text = QVBoxLayout()
        header_text.addWidget(heading)
        header_text.addWidget(hint)
        header.addLayout(header_text, 1)
        self.release_badge = QLabel(f"{__version__} · {VARIANT_BADGE}")
        self.release_badge.setObjectName("releaseBadge")
        self.release_badge.setAccessibleName(
            DISPLAY_NAME)
        header.addWidget(self.release_badge)
        self.b_review_queue = _accessible(
            QPushButton("Черга перевірки…"), "reviewQueueButton",
            "Черга результатів, що потребують рішення",
            "Показує конфлікти, недоступні джерела, подібні папки й "
            "позначені пакети без зміни файлів.")
        self.b_review_queue.clicked.connect(self.show_review_queue)
        header.addWidget(self.b_review_queue)
        layout.addLayout(header)

        self.summary = QLabel("Результатів сканування ще немає.")
        self.summary.setObjectName("resultSummary")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary)

        self.session_rescan_panel = QFrame()
        self.session_rescan_panel.setObjectName("sessionRescanPanel")
        rescan_layout = QHBoxLayout(self.session_rescan_panel)
        rescan_layout.setContentsMargins(12, 9, 12, 9)
        rescan_layout.setSpacing(12)
        self.session_rescan_text = QLabel(
            "Це збережений знімок. Пару подібних тек можна злити просто "
            "зараз: DupScan пересканує РІВНО обрані A і B перед дією "
            "(ПКМ на парі → «Перевірити й злити»). Повний рескан потрібен "
            "лише для дій над групами дублікатів.")
        self.session_rescan_text.setObjectName("sessionRescanText")
        self.session_rescan_text.setWordWrap(True)
        self.session_rescan_text.setAccessibleName(
            "Сесія потребує актуалізації")
        rescan_layout.addWidget(self.session_rescan_text, 1)
        self.b_refresh_session = _accessible(
            QPushButton("Інтелектуальний рескан…"),
            "refreshLoadedSessionButton",
            "Запустити інтелектуальний рескан завантаженої сесії",
            "У фоні перевіряє поточну структуру всіх коренів, повторно "
            "використовує лише валідні BLAKE3 та створює нову сесію.")
        self.b_refresh_session.clicked.connect(self.refresh_loaded_session)
        rescan_layout.addWidget(self.b_refresh_session)
        self.session_rescan_panel.setVisible(False)
        layout.addWidget(self.session_rescan_panel)

        self.result_quality_notice = QFrame()
        self.result_quality_notice.setObjectName("resultQualityNotice")
        quality_layout = QHBoxLayout(self.result_quality_notice)
        quality_layout.setContentsMargins(11, 8, 11, 8)
        self.result_quality_text = QLabel()
        self.result_quality_text.setObjectName("resultQualityText")
        self.result_quality_text.setWordWrap(True)
        self.result_quality_text.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        self.result_quality_text.setAccessibleName(
            "Попередження про неповні результати")
        quality_layout.addWidget(self.result_quality_text, 1)
        self.result_quality_notice.setVisible(False)
        layout.addWidget(self.result_quality_notice)

        self.results_operation_banner = QFrame()
        self.results_operation_banner.setObjectName("resultsOperationBanner")
        result_operation_layout = QHBoxLayout(self.results_operation_banner)
        result_operation_layout.setContentsMargins(11, 8, 11, 8)
        self.results_operation_text = ElidingLabel()
        self.results_operation_text.setObjectName("resultsOperationText")
        self.results_operation_text.setTextInteractionFlags(
            Qt.TextSelectableByMouse)
        self.results_operation_text.setAccessibleName(
            "Поточний стан безпечної операції")
        result_operation_layout.addWidget(self.results_operation_text, 1)
        # Місце банера зарезервоване завжди: вставка й вилучення рядка
        # layout смикали вікно на кожній операції.
        self.results_operation_banner.setVisible(True)
        self.results_operation_banner.setProperty("active", "false")
        layout.addWidget(self.results_operation_banner)

        def dates(path: str):
            if self.result and path in self.result.file_meta:
                info = self.result.file_meta[path]
                return info.btime_ns, info.mtime_ns
            return self._dir_dates.get(path)

        self.tabs = QTabWidget()
        self.m_files = GroupModel(dates)
        self.m_dirs = GroupModel(dates)
        self.m_sim = SimModel()
        self.m_sim.tags = dict(self._similarity_tags)
        self.m_files.keeper_fn = self._choose_keeper
        self.m_dirs.keeper_fn = self._choose_keeper
        # Тека-еталон, перший шар: позначка не ставиться вже в моделі.
        self._load_reference_cache()
        for protected_model in (self.m_files, self.m_dirs, self.m_sim):
            protected_model.protected_checker = self._reference_blocked
        self.v_files = self._tree(self.m_files, "files")
        self.v_dirs = self._tree(self.m_dirs, "dirs")
        self.v_sim = self._tree(self.m_sim, "similarity")
        # «Кластери тек» — read-only, не через
        # _tree() (та обв'язка кличе GroupModel/SimModel-специфічні сигнали:
        # sortStarted/set_expanded/load_more_at — ClusterModel їх свідомо не
        # має, без пагінації й без чекбоксів; окрема, простіша обв'язка).
        self.m_clusters = ClusterModel(self)
        self._clusters_ctl = ClustersTabController(
            parent=self,
            model=self.m_clusters,
            sim_model=self.m_sim,
            sim_view=self.v_sim,
            tabs=self.tabs,
            status=self.status,
            model_views=self._model_views,
            view_names=self._view_names,
            column_base_widths=self._column_base_widths,
        )
        self.v_clusters = self._clusters_ctl.view
        # «Схожі фото (підказка)» — та сама
        # проста, НЕ через _tree(), обв'язка, що й кластери; окремий воркер
        # (perceptual_worker), бо це ОКРЕМИЙ, опційний прохід (наш core.scan
        # фото не хешує перцептивно).
        self.m_perceptual = PerceptualModel(self)
        self.perceptual_worker: PerceptualScanWorker | None = None
        self._perceptual_ctl = PerceptualTabController(
            parent=self,
            model=self.m_perceptual,
            tabs=self.tabs,
            get_result=lambda: self.result,
            get_worker=lambda: self.perceptual_worker,
            set_worker=lambda w: setattr(self, "perceptual_worker", w),
            set_scanned_for=lambda r: setattr(self, "_perceptual_scanned_for", r),
            model_views=self._model_views,
            view_names=self._view_names,
            column_base_widths=self._column_base_widths,
        )
        self.v_perceptual = self._perceptual_ctl.view
        self.b_perceptual_scan = self._perceptual_ctl.b_scan
        self.b_perceptual_pause = self._perceptual_ctl.b_pause
        self.b_perceptual_cancel = self._perceptual_ctl.b_cancel
        self.l_perceptual_status = self._perceptual_ctl.l_status
        # Вкладка «Проблеми» — та сама проста, НЕ
        # через _tree(), обв'язка, що кластери/перцептив:
        # read-only, без sortStarted/set_expanded/load_more_at.
        self.m_problems = ProblemsModel(self)
        self._problems_ctl = ProblemsTabController(
            parent=self,
            model=self.m_problems,
            status=self.status,
            bg_run=lambda *args, **kwargs: self._bg_run(*args, **kwargs),
            open_result_path=lambda path, opener: self._open_result_path(path, opener),
            model_views=self._model_views,
            view_names=self._view_names,
            column_base_widths=self._column_base_widths,
        )
        self.v_problems = self._problems_ctl.view
        self.b_problems_namefix_bulk = self._problems_ctl.b_namefix_bulk
        self.l_problems_status = self._problems_ctl.l_status
        self.tabs.addTab(
            self._dup_tab(self.v_files, self.m_files, categories=True),
            "Файли-дублікати")
        self.tabs.addTab(
            self._dup_tab(self.v_dirs, self.m_dirs), "Папки-дублікати")
        self.tabs.addTab(self._sim_tab(self.v_sim), "Подібність папок")
        self.tabs.addTab(self._clusters_ctl.tab, "Кластери тек")
        self.tabs.addTab(self._perceptual_ctl.tab, "Схожі фото (підказка)")
        self.tabs.addTab(self._problems_ctl.tab, "Проблеми")
        self.tabs.currentChanged.connect(self._tab_changed)
        self.v_files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.v_dirs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.v_files.customContextMenuRequested.connect(
            lambda pos: self._duplicate_menu(self.v_files, self.m_files, pos))
        self.v_dirs.customContextMenuRequested.connect(
            lambda pos: self._duplicate_menu(self.v_dirs, self.m_dirs, pos))
        self.v_sim.setContextMenuPolicy(Qt.CustomContextMenu)
        self.v_sim.customContextMenuRequested.connect(self._sim_menu)
        self.v_sim.expanded.connect(self._sim_expanded)
        self.v_clusters.setContextMenuPolicy(Qt.CustomContextMenu)
        self.v_clusters.customContextMenuRequested.connect(self._clusters_menu)
        self.v_perceptual.setContextMenuPolicy(Qt.CustomContextMenu)
        self.v_perceptual.customContextMenuRequested.connect(
            self._perceptual_menu)
        self.v_problems.setContextMenuPolicy(Qt.CustomContextMenu)
        self.v_problems.customContextMenuRequested.connect(self._problems_menu)
        layout.addWidget(self.tabs, 1)

        self.selection_bar = QFrame()
        self.selection_bar.setObjectName("selectionActionBar")
        selected_row = QHBoxLayout(self.selection_bar)
        selected_row.setContentsMargins(11, 7, 9, 7)
        self.selection_summary = QLabel("Позначено до Кошика: 0")
        self.selection_summary.setAccessibleName("Підсумок вибраних елементів")
        self.selection_summary.setToolTip(
            "Галочка формує пакет до Кошика. Синій рядок відкриває деталі "
            "і сам по собі не додається до пакета.")
        self.b_trash_marked = QPushButton("Переглянути пакет до Кошика…")
        self.b_trash_marked.setIcon(
            self.style().standardIcon(getattr(
                QStyle.StandardPixmap, "SP_TrashIcon",
                QStyle.StandardPixmap.SP_DialogDiscardButton)))
        self.b_trash_marked.setEnabled(False)
        self.b_trash_marked.setAccessibleName(
            "Переглянути всі позначені на поточній вкладці перед Кошиком")
        self.b_trash_marked.setToolTip(
            "Покаже остаточний список і повторно перевірить кожен дублікат.")
        self.b_trash_marked.clicked.connect(self._trash_marked_on_current_tab)
        selected_row.addWidget(self.selection_summary)
        selected_row.addStretch(1)
        selected_row.addWidget(self.b_trash_marked)
        self.selection_bar.setVisible(False)
        layout.addWidget(self.selection_bar)
        return page

    def _build_compare_task(self) -> QWidget:
        page = QWidget()
        page.setObjectName("compareTaskPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 26, 30, 24)
        layout.setSpacing(14)
        heading, hint = self._page_heading(
            "Порівняти теки A / B",
            "Це окремий сценарій для двох конкретних тек. DupScan чітко "
            "зберігає їхні ролі й перед злиттям перевіряє лише цю пару.")
        layout.addWidget(heading)
        layout.addWidget(hint)
        roles = QHBoxLayout()
        for title, text in (
                ("Тека A", "Перше джерело. Її повний шлях буде показано "
                 "у заголовку та кожному напрямку злиття."),
                ("Тека B", "Друге джерело. Жоден напрямок злиття не "
                 "обирається автоматично.")):
            panel = QFrame()
            panel.setProperty("section", True)
            panel_layout = QVBoxLayout(panel)
            role_title = QLabel(title)
            role_title.setStyleSheet("font-size: 17px; font-weight: 600;")
            role_text = QLabel(text)
            role_text.setWordWrap(True)
            role_text.setProperty("pageHint", True)
            panel_layout.addWidget(role_title)
            panel_layout.addWidget(role_text)
            panel_layout.addStretch(1)
            roles.addWidget(panel, 1)
        layout.addLayout(roles)
        b_start_compare = _accessible(
            QPushButton("Обрати теки A і B…"), "startCompareButton",
            "Обрати й порівняти теку A і теку B",
            "Спочатку відкриє вибір теки A, потім теки B.")
        b_start_compare.setDefault(True)
        b_start_compare.clicked.connect(self.compare_two_folders)
        actions = QHBoxLayout()
        actions.addWidget(b_start_compare)
        actions.addStretch(1)
        layout.addLayout(actions)
        compare_note = QLabel(
            "Злиття ніколи не запускається одразу після порівняння: спочатку "
            "показується напрямок, план, вибіркова перевірка й підтвердження.")
        compare_note.setWordWrap(True)
        compare_note.setProperty("pageHint", True)
        layout.addWidget(compare_note)
        layout.addStretch(1)
        return page

    def _show_task(self, task: str) -> None:
        # UI ownership, not QThread teardown timing, defines navigation.
        # done() can arrive a few milliseconds before isRunning() flips false.
        scan_busy = hasattr(self, "b_scan") and not self.b_scan.isEnabled()
        state = workflow_ui.navigation_state(
            task, has_result=self.result is not None, scan_busy=scan_busy)
        self.nav_results.setEnabled(state.results_enabled)
        self.nav_compare.setEnabled(not state.scan_busy)
        page = self._task_pages[state.active_task]
        self.task_stack.setCurrentWidget(page)
        buttons = {
            workflow_ui.TASK_SCAN: self.nav_scan,
            workflow_ui.TASK_RESULTS: self.nav_results,
            workflow_ui.TASK_COMPARE: self.nav_compare,
        }
        buttons[state.active_task].setChecked(True)
        if state.active_task == workflow_ui.TASK_RESULTS:
            self._update_selection_summary()
            # Усі 6 вкладок мусять бути в цьому кортежі — без будь-якої
            # з них currentIndex() на пропущеній вкладці (3, 4 чи 5) кинув
            # би IndexError тут.
            self._fill_columns(
                (self.v_files, self.v_dirs, self.v_sim, self.v_clusters,
                 self.v_perceptual, self.v_problems)
                [self.tabs.currentIndex()])

    def _set_operation_controls_visible(self, visible: bool) -> None:
        self.operation_controls.setVisible(visible)

    def _set_results_operation(self, text: str = "", *, visible: bool) -> None:
        # Місце банера зарезервоване завжди: показ і приховування більше не
        # вставляють і не забирають рядок layout, тому вікно не смикається.
        self.results_operation_text.setText(text if visible else "")
        banner = self.results_operation_banner
        banner.setProperty("active", "true" if visible else "false")
        banner.style().unpolish(banner)
        banner.style().polish(banner)

    def _update_result_quality(self, result: core.ScanResult | None) -> None:
        errors = tuple(result.errors) if result else ()
        total_errors = _problem_count(result)
        if not total_errors:
            self.result_quality_text.clear()
            self.result_quality_notice.setVisible(False)
            return
        grouped: dict[str, int] = {}
        for message in errors:
            title, _advice = product.classify_problem(message)
            grouped[title] = grouped.get(title, 0) + 1
        breakdown = " · ".join(
            f"{title}: {count:,}" for title, count in grouped.items())
        self.result_quality_text.setText(
            f"Результат неповний: {total_errors:,} шляхів не прочитано "
            f"({breakdown}). Великі схожі теки можуть бути недооцінені. "
            "Деталі є у «Черзі перевірки»; перед злиттям DupScan усе одно "
            "виконає свіжу вибіркову перевірку.")
        self.result_quality_notice.setVisible(True)

    def _refresh_profile_summary(self) -> None:
        self._settings_ctl.refresh_profile_summary()

    def _choose_keeper(self, paths: list[str]) -> str | None:
        metadata: dict[str, object] = {}
        for path in paths:
            if self.result and path in self.result.file_meta:
                metadata[path] = self.result.file_meta[path]
            else:
                dates = self._dir_dates.get(path)
                if dates:
                    metadata[path] = {"mtime_ns": dates[1]}
        return preferences.choose_keeper(paths, metadata, self._selection_rules)

    def show_preferences(self) -> None:
        self._settings_ctl.show_preferences()

    def _tab_changed(self, index: int) -> None:
        views = (
            self.v_files, self.v_dirs, self.v_sim, self.v_clusters,
            self.v_perceptual, self.v_problems)
        if 0 <= index < len(views):
            self._fill_columns(views[index])
        self._update_selection_summary()

    def _selection_paths(self) -> set[str]:
        return set(self.m_files.checked) | set(self.m_dirs.checked) | set(self.m_sim.checked)

    def _update_review_queue_button(self) -> None:
        # Large batch selection emits incremental model signals every UI
        # quantum. The final summary refreshes the queue once; intermediate
        # label churn must not consume the 8 ms responsiveness budget.
        if self._batch_selecting:
            return
        if self.result is None:
            self.b_review_queue.setText("Черга перевірки…")
            return
        similarity_review = sum(
            self.m_sim.tags.get(self.m_sim.pair_key(pair), "До перевірки")
            != "Схвалено"
            for pair in self.m_sim.pairs
        )
        total = (
            _problem_count(self.result)
            + similarity_review
            + len(self.m_files.checked)
            + len(self.m_dirs.checked)
            + len(self.m_sim.checked)
            + int(self.result.partial or not self.result.live)
        )
        self.b_review_queue.setText(
            f"Черга перевірки ({total:,})…" if total else "Черга перевірки…")

    def _active_model_view(self):
        index = self.tabs.currentIndex()
        models = (self.m_files, self.m_dirs, self.m_sim)
        views = (self.v_files, self.v_dirs, self.v_sim)
        if 0 <= index < len(models):
            return models[index], views[index]
        return None, None

    def _current_check_index(self):
        """Return the checkable cell represented by the blue current row."""
        model, view = self._active_model_view()
        if model is None or view is None:
            return None, QModelIndex(), None
        current = view.currentIndex()
        if not current.isValid() or current.internalId() == 0:
            return model, QModelIndex(), None
        if isinstance(model, GroupModel):
            check_index = current.siblingAtColumn(0)
            return model, check_index, model._path(check_index)
        if current.column() not in (0, 1):
            return model, QModelIndex(), None
        if not model.flags(current) & Qt.ItemIsUserCheckable:
            return model, QModelIndex(), None
        return model, current, model.path_at(current)

    def _update_selection_summary(self) -> None:
        if not hasattr(self, "tabs"):
            return
        index = self.tabs.currentIndex()
        if index == 0:
            selected = set(self.m_files.checked)
            stats = self.m_files.selection_stats()
            total = stats["bytes"]
            groups_count = stats["groups"]
        elif index == 1:
            selected = set(self.m_dirs.checked)
            stats = self.m_dirs.selection_stats()
            total = stats["bytes"]
            groups_count = stats["groups"]
        elif index == 2:
            selected = set(self.m_sim.checked)
            total = sum(self.result.file_meta[path].size
                        for path in selected
                        if self.result and path in self.result.file_meta)
            groups_count = len({
                model_row for model_row, pair in enumerate(self.m_sim.pairs)
                if selected.intersection(
                    {pair.dir_a, pair.dir_b}
                    | {path for _size, a, b in pair.shared for path in (a, b)}
                )
            })
        else:
            # «Кластери тек» (3) і «Схожі
            # фото (підказка)» (4) — структурно read-only, немає .checked.
            # Раніше цей else-catchall трактував БУДЬ-ЯКИЙ index>=2 як
            # вкладку подібності (index==2) — з новими вкладками це б хибно
            # показувало позначки з подібності на вкладках, де їх нема.
            selected = set()
            total = 0
            groups_count = 0
        count = len(selected)
        if count:
            self.selection_summary.setText(
                f"Позначено до Кошика: {count:,} · груп: {groups_count:,} · "
                f"{human(total)}")
        else:
            self.selection_summary.setText("Позначено до Кошика: 0")
        self.b_trash_marked.setText(
            f"Переглянути пакет ({count:,}) до Кошика…"
            if count else "Переглянути пакет до Кошика…")
        self.selection_bar.setVisible(
            workflow_ui.selection_bar_visible(count))
        self._update_review_queue_button()
        available = bool(
            self.result and self.result.live and not self.result.partial
            and not self._batch_selecting)
        self.b_trash_marked.setEnabled(bool(count and available))
        self._update_action_state()

    def _update_action_state(self) -> None:
        if not hasattr(self, "action_trash_current"):
            return
        model, _view = self._active_model_view()
        _current_model, check_index, path = self._current_check_index()
        available = bool(
            self.result and self.result.live and not self.result.partial
            and not self._batch_selecting)
        current_available = bool(available and path and check_index.isValid())
        marked = len(model.checked) if model is not None else 0
        self.action_trash_current.setEnabled(current_available)
        self.action_toggle_trash_mark.setEnabled(current_available)
        self.action_trash_marked.setEnabled(bool(available and marked))
        self.action_trash_marked.setText(
            f"Перемістити позначені ({marked:,}) у Кошик…"
            if marked else "Перемістити позначені у Кошик…")
        if current_available:
            checked = path in model.checked
            self.action_toggle_trash_mark.setText(
                "Зняти позначку для Кошика" if checked
                else "Позначити для Кошика")
        else:
            self.action_toggle_trash_mark.setText("Позначити для Кошика")

    def _create_menus(self) -> None:
        menu_file = self.menuBar().addMenu("Файл")
        action_add = QAction("Додати джерела…", self)
        action_add.setShortcut(QKeySequence.StandardKey.Open)
        action_add.triggered.connect(self.add_dirs)
        action_scan = QAction("Сканувати", self)
        action_scan.setShortcut(QKeySequence("Ctrl+Return"))
        action_scan.triggered.connect(self.start_scan)
        action_compare = QAction("Порівняти дві теки…", self)
        action_compare.triggered.connect(self.compare_two_folders)
        menu_file.addActions((action_add, action_scan, action_compare))
        menu_file.addSeparator()
        export_menu = menu_file.addMenu("Експортувати звіт")
        export_menu.addAction("CSV…", lambda: self.export_report("csv"))
        export_menu.addAction("HTML…", lambda: self.export_report("html"))
        menu_file.addSeparator()
        action_quit = QAction("Вийти", self)
        action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        action_quit.triggered.connect(self.close)
        menu_file.addAction(action_quit)

        menu_edit = self.menuBar().addMenu("Редагування")
        action_preferences = QAction("Налаштування…", self)
        action_preferences.setShortcut(QKeySequence.StandardKey.Preferences)
        action_preferences.triggered.connect(self.show_preferences)
        menu_edit.addAction(action_preferences)

        menu_actions = self.menuBar().addMenu("Дії")
        trash_icon = self.style().standardIcon(getattr(
            QStyle.StandardPixmap, "SP_TrashIcon",
            QStyle.StandardPixmap.SP_DialogDiscardButton))
        self.action_toggle_trash_mark = QAction("Позначити для Кошика", self)
        self.action_toggle_trash_mark.triggered.connect(self._toggle_current_trash_mark)
        self.action_trash_current = QAction(
            trash_icon, "Перемістити вибраний дублікат у Кошик…", self)
        self.action_trash_current.setIconText("Вибраний у Кошик…")
        self.action_trash_current.setShortcut(QKeySequence("Meta+Backspace"))
        self.action_trash_current.setToolTip(
            "Показати перевірку й перемістити синій вибраний дублікат у Кошик")
        self.action_trash_current.triggered.connect(self._trash_current_duplicate)
        self.action_trash_marked = QAction(
            trash_icon, "Перемістити позначені у Кошик…", self)
        self.action_trash_marked.setToolTip(
            "Показати остаточний review і перемістити всі позначені дублікати "
            "на поточній вкладці у Кошик")
        self.action_trash_marked.triggered.connect(self._trash_marked_on_current_tab)
        action_select_filtered = QAction(
            "Позначити поточний фільтр безпечно", self)
        action_select_filtered.setShortcut(QKeySequence("Meta+Shift+A"))
        action_select_filtered.setToolTip(
            "Позначити всі результати поточного фільтра, включно з "
            "незавантаженими сторінками, лишивши survivor у кожній групі")
        action_select_filtered.triggered.connect(self._select_active_filtered)
        menu_actions.addAction(action_select_filtered)
        menu_actions.addSeparator()
        menu_actions.addAction(self.action_toggle_trash_mark)
        menu_actions.addSeparator()
        menu_actions.addAction(self.action_trash_current)
        menu_actions.addAction(self.action_trash_marked)

        menu_view = self.menuBar().addMenu("Вигляд")
        for index, title in enumerate(
                ("Файли-дублікати", "Папки-дублікати", "Подібність папок")):
            action = QAction(title, self)
            action.setShortcut(QKeySequence(f"Ctrl+{index + 1}"))
            action.triggered.connect(lambda _checked=False, i=index: self.tabs.setCurrentIndex(i))
            menu_view.addAction(action)
        menu_view.addSeparator()
        action_inspector = QAction("Показати або сховати деталі", self)
        action_inspector.setShortcut(QKeySequence("Meta+I"))
        action_inspector.triggered.connect(self._toggle_active_inspector)
        menu_view.addAction(action_inspector)

        menu_help = self.menuBar().addMenu("Довідка")
        menu_help.addAction("Як користуватися DupScan", self.show_onboarding)
        menu_help.addAction("Конфіденційність", self.show_privacy)
        menu_help.addAction("Перевірити оновлення…", self.check_updates)
        menu_help.addAction("Експортувати діагностику…", self.export_diagnostics)
        menu_help.addAction("Підтримка…", self.show_support)
        menu_help.addSeparator()
        menu_help.addAction("Про DupScan", self.show_about)

        toolbar = self.addToolBar("Основні дії")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toolbar.addAction(action_add)
        toolbar.addAction(action_scan)
        toolbar.addAction(action_compare)
        toolbar.addSeparator()
        toolbar.addAction(self.action_trash_current)
        # Task pages own the visible primary commands. The QAction container
        # remains for menu/shortcut compatibility but must not duplicate them.
        toolbar.hide()

        for view in (
                self.v_files, self.v_dirs, self.v_sim, self.v_clusters,
                self.v_perceptual, self.v_problems):
            view.selectionModel().currentChanged.connect(
                lambda _current, _previous: self._update_action_state())
        self._update_selection_summary()

    def _apply_accessibility(self) -> None:
        _accessible(
            self.folders, "scanFolders", "Теки для сканування",
            "Чекбокс визначає участь теки; кожен рядок містить повний шлях.")
        _accessible(
            self.b_scan, "scanButton", "Сканувати",
            "Почати безпечний пошук дублікатів")
        _accessible(
            self.b_pause, "pauseButton", "Пауза або продовження",
            "Призупинити або продовжити поточну фонову операцію")
        _accessible(
            self.b_cancel, "cancelButton", "Скасувати операцію",
            "Скасувати поточну операцію без нової зміни файлів")
        _accessible(self.bar, "scanProgress", "Прогрес поточної операції")
        _accessible(
            self.profile_summary, "scanProfile", "Активний профіль сканування",
            "Профіль змінюється через Налаштування у бічній панелі.")
        _accessible(
            self.b_preferences, "preferencesButton", "Налаштування сканування")
        _accessible(
            self.status, "operationStatus", "Стан поточної операції")
        _accessible(
            self.summary, "scanSummary", "Підсумок результатів сканування")
        _accessible(self.tabs, "resultTabs", "Результати сканування")
        _accessible(
            self.b_trash_marked, "trashMarkedButton",
            "Перемістити всі позначені на поточній вкладці у Кошик",
            "Спочатку покаже остаточний список і повторно перевірить кожен дублікат.")
        for view, object_name, name in (
                (self.v_files, "duplicateFilesTree", "Дублікати файлів"),
                (self.v_dirs, "duplicateFoldersTree", "Дублікати папок"),
                (self.v_sim, "similarFoldersTree", "Подібні папки")):
            _accessible(
                view, object_name, name,
                "Повний шлях вибраного рядка доступний у підказці та панелі деталей.")

    def _show_onboarding_if_needed(self) -> None:
        if "--smoke" in sys.argv or os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        if not bool(self._settings.value("onboarding/completed", False, type=bool)):
            self.show_onboarding()

    def show_onboarding(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Ласкаво просимо до DupScan")
        dialog.resize(610, 430)
        layout = QVBoxLayout(dialog)
        title = QLabel("Безпечно звільняйте місце без здогадок")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        layout.addWidget(title)
        text = QTextBrowser()
        text.setHtml(
            "<ol><li><b>Додайте джерела</b> — папки й диски обираються в "
            "одному вікні. Профіль змінюється в Налаштуваннях.</li>"
            "<li><b>Скануйте.</b> Файли спочатку групуються за розміром, а "
            "кандидати повністю перевіряються BLAKE3.</li>"
            "<li><b>Розумно позначте</b> зайві копії та перегляньте їх через "
            "Quick Look.</li><li><b>Перемістіть у Кошик.</b> Безповоротного "
            "видалення немає; без живої незалежної копії операцію заблоковано.</li></ol>"
            "<p>Сканування й звіти виконуються локально. DupScan не передає "
            "назви або вміст ваших файлів.</p>")
        layout.addWidget(text, 1)
        button = QPushButton("Почати роботу")
        button.clicked.connect(dialog.accept)
        layout.addWidget(button)
        dialog.exec()
        self._settings.setValue("onboarding/completed", True)

    def show_privacy(self) -> None:
        QMessageBox.information(
            self, "Конфіденційність DupScan",
            "DupScan сканує й хешує файли лише локально. Дані не надсилаються "
            "в мережу. Перевірка оновлень виконується тільки вручну; діагностика "
            "редагує приватні шляхи, якщо ви явно не дозволили їх включити.")

    def show_about(self) -> None:
        QMessageBox.about(
            self, "Про DupScan",
            f"{DISPLAY_NAME}\n\n"
            "Безпечний пошук точних дублікатів і "
            "порівняння папок для macOS. Повна перевірка BLAKE3, Кошик замість "
            "безповоротного видалення, локальна обробка даних.")

    def show_support(self) -> None:
        support_url = updates.configured_support_url()
        if support_url:
            answer = QMessageBox.question(
                self, "Підтримка DupScan",
                "Перед зверненням можна експортувати приватну діагностику через "
                "меню «Довідка». Відкрити захищену сторінку підтримки?\n\n"
                f"{support_url}")
            if answer == QMessageBox.StandardButton.Yes:
                subprocess.Popen(["open", support_url])
            return
        QMessageBox.information(
            self, "Підтримка DupScan",
            "Якщо виникла проблема, відкрийте «Центр проблем», а потім "
            "експортуйте діагностику через меню «Довідка». Архів не містить "
            "вмісту файлів; шляхи додаються лише після окремої згоди. "
            "У цій збірці контакт підтримки ще не налаштовано.")

    def show_problems(self) -> None:
        errors = list(self.result.errors) if self.result else []
        ProblemsDialog(self, errors, _problem_count(self.result)).exec()

    def show_review_queue(self) -> None:
        """Show unresolved work as navigation only; never mutate selection/files."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Черга перевірки — DupScan")
        dialog.resize(680, 430)
        layout = QVBoxLayout(dialog)
        intro = QLabel(
            "Черга збирає місця, де потрібне рішення людини. "
            "Перехід або фільтр нічого не переміщує до Кошика.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        queue = QListWidget()
        _accessible(
            queue, "reviewQueue", "Категорії результатів для перевірки")
        errors = _problem_count(self.result)
        similarity_review = sum(
            self.m_sim.tags.get(self.m_sim.pair_key(pair), "До перевірки")
            != "Схвалено"
            for pair in self.m_sim.pairs
        )
        selected = len(self._selection_paths())
        external_selected = sum(
            path.startswith("/Volumes/") for path in self._selection_paths())
        rows = [
            (f"Проблеми сканування · {errors}", ("problems",)),
            (f"Подібні папки до перевірки · {similarity_review}",
             ("tab", 2, None)),
            (f"Позначені кандидати · {selected}",
             ("tab", self.tabs.currentIndex(), None)),
            (f"Зовнішні / мережеві кандидати · {external_selected}",
             ("tab", 0, "network")),
        ]
        if self.result and (self.result.partial or not self.result.live):
            rows.insert(
                0, ("Сесія потребує свіжої перевірки перед змінами",
                    ("status",)))
        for label, target in rows:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, target)
            queue.addItem(item)
        queue.setCurrentRow(0)
        layout.addWidget(queue, 1)
        actions = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        go = actions.addButton(
            "Перейти", QDialogButtonBox.ButtonRole.ActionRole)

        def navigate() -> None:
            current = queue.currentItem()
            target = current.data(Qt.UserRole) if current else None
            if not target:
                return
            if target[0] == "problems":
                dialog.accept()
                self.show_problems()
                return
            if target[0] == "tab":
                self.tabs.setCurrentIndex(target[1])
                if len(target) > 2 and target[2]:
                    model, _view = self._active_model_view()
                    box = self._preset_boxes.get(model)
                    if box is not None:
                        box.setCurrentIndex(max(0, box.findData(target[2])))
                dialog.accept()
                return
            self.status.setText(
                "Завантажена або часткова сесія: перед дією буде перевірено "
                "лише вибраний scope.")
            dialog.accept()

        go.clicked.connect(navigate)
        actions.rejected.connect(dialog.reject)
        layout.addWidget(actions)
        dialog.exec()

    def export_report(self, kind: str) -> None:
        self._reports.export_report(kind)

    def check_updates(self) -> None:
        self._reports.check_updates()

    def export_diagnostics(self) -> None:
        self._reports.export_diagnostics()

    # ---- дерева: моделі самі сортують готові ключі за O(n log n)
    def _tree(self, model, settings_name: str) -> QTreeView:
        v = ResultTreeView()
        v.setModel(model)
        self._model_views[model] = v
        v.setUniformRowHeights(True)
        v.setAlternatingRowColors(True)
        h = v.header()
        h.setSortIndicator(-1, Qt.AscendingOrder)
        v.setSortingEnabled(True)
        h.setSectionsMovable(True)          # колонки можна перетягувати
        h.setStretchLastSection(False)
        self._column_base_widths[v] = [h.sectionSize(c)
                                       for c in range(model.columnCount())]
        h.sectionResized.connect(
            lambda c, _old, new, v=v: self._remember_column_width(v, c, new))
        h.setContextMenuPolicy(Qt.CustomContextMenu)  # ПКМ: вкл/викл колонок
        h.customContextMenuRequested.connect(
            lambda pos, v=v: self._header_menu(v, pos))
        model.sortStarted.connect(lambda v=v: self._capture_sort_state(v))
        model.sortFinished.connect(lambda v=v: self._restore_sort_state(v))
        if isinstance(model, GroupModel):
            v.expanded.connect(lambda idx, m=model: m.set_expanded(idx, True))
            v.collapsed.connect(lambda idx, m=model: m.set_expanded(idx, False))
            model.modelAboutToBeReset.connect(model._expanded.clear)
        def activate(idx, model=model):
            if hasattr(model, "load_more_at") and model.load_more_at(idx):
                return
            self._reveal_idx(idx)

        v.activated.connect(activate)
        v.toggleCurrentRequested.connect(self._toggle_current_trash_mark)
        v.focusSearchRequested.connect(
            lambda m=model: self._focus_search(m))
        v.clearSearchRequested.connect(
            lambda m=model: self._clear_search(m))
        self._view_names[v] = settings_name
        self._restore_view_settings(v, settings_name)
        return v

    def _restore_view_settings(self, view: QTreeView, name: str) -> None:
        h = view.header()
        raw_widths = str(self._settings.value(f"views/{name}/widths", ""))
        try:
            widths = [int(v) for v in raw_widths.split(",") if v]
        except ValueError:
            widths = []
        if len(widths) == view.model().columnCount():
            self._resizing_columns.add(view)
            try:
                for c, width in enumerate(widths):
                    h.resizeSection(c, max(h.minimumSectionSize(), width))
                self._column_base_widths[view] = widths
            finally:
                self._resizing_columns.discard(view)
        raw_order = str(self._settings.value(f"views/{name}/order", ""))
        try:
            logical_order = [int(v) for v in raw_order.split(",") if v]
        except ValueError:
            logical_order = []
        if sorted(logical_order) == list(range(view.model().columnCount())):
            for visual, logical in enumerate(logical_order):
                current = h.visualIndex(logical)
                if current != visual:
                    h.moveSection(current, visual)
        hidden = {int(v) for v in str(
            self._settings.value(f"views/{name}/hidden", "")).split(",")
                  if v.isdigit()}
        for c in hidden:
            if c != 0 and c < view.model().columnCount():
                view.setColumnHidden(c, True)
        # QSettings.value() у стабах повертає object (Qt
        # зберігає QVariant, будь-який тип) — на ini-бекенді реально
        # рядок або сам дефолт, int() безпечно приймає обидва в рантаймі.
        sort_col = int(self._settings.value(  # type: ignore[call-overload]
            f"views/{name}/sort_column", -1))
        sort_order = int(self._settings.value(  # type: ignore[call-overload]
            f"views/{name}/sort_order", 0))
        if 0 <= sort_col < view.model().columnCount():
            order = Qt.DescendingOrder if sort_order else Qt.AscendingOrder
            h.setSortIndicator(sort_col, order)
            view.model().sort(sort_col, order)

    def _save_view_settings(self) -> None:
        for view, name in self._view_names.items():
            widths = self._column_base_widths.get(view, [])
            self._settings.setValue(
                f"views/{name}/widths", ",".join(str(v) for v in widths))
            hidden = [str(c) for c in range(view.model().columnCount())
                      if view.isColumnHidden(c)]
            self._settings.setValue(f"views/{name}/hidden", ",".join(hidden))
            h = view.header()
            self._settings.setValue(
                f"views/{name}/order",
                ",".join(str(h.logicalIndex(v))
                         for v in range(h.count())))
            self._settings.setValue(f"views/{name}/sort_column",
                                    h.sortIndicatorSection())
            self._settings.setValue(
                f"views/{name}/sort_order",
                1 if h.sortIndicatorOrder() == Qt.DescendingOrder else 0)
            model = view.model()
            edit = self._search_boxes.get(model)
            if edit is not None:
                self._settings.setValue(f"views/{name}/query", edit.text())
            category = self._category_boxes.get(model)
            if category is not None:
                self._settings.setValue(
                    f"views/{name}/category", category.currentData())
            preset = self._preset_boxes.get(model)
            if preset is not None:
                self._settings.setValue(
                    f"views/{name}/preset", preset.currentData())
            density = str(view.property("dupscanDensity") or "compact")
            try:
                density = scale_ui.validate_density(density)
            except ValueError:
                density = "compact"
            self._settings.setValue(f"views/{name}/density", density)
            inspector = self._inspector_buttons.get(model)
            if inspector is not None:
                self._settings.setValue(
                    f"views/{name}/inspector_visible", inspector.isChecked())
            splitter = self._splitters.get(model)
            if splitter is not None:
                sizes = splitter.sizes()
                if len(sizes) > 1 and sizes[1] > 0:
                    self._inspector_last_size[model] = sizes[1]
            self._settings.setValue(
                f"views/{name}/inspector_size",
                self._inspector_last_size.get(model, 260))
        self._settings.sync()

    def _capture_sort_state(self, view: QTreeView) -> None:
        model = cast(_TreeModel, view.model())
        expanded = (set(model._expanded) if isinstance(model, GroupModel) else {
            item_id for row in range(model.rowCount())
            if view.isExpanded(model.index(row, 0))
            for item_id in (model.root_id(row),) if item_id is not None
        })
        current = model.item_key(view.currentIndex())
        self._sort_view_state[view] = (
            expanded, current, view.verticalScrollBar().value())
        self._sort_previous_status[view] = self.status.text()
        self.status.setText("Сортую результати…")
        QApplication.setOverrideCursor(Qt.WaitCursor)

    def _restore_sort_state(self, view: QTreeView) -> None:
        state = self._sort_view_state.pop(view, None)
        if state is None:
            return
        expanded, current, scroll = state
        model = cast(_TreeModel, view.model())
        if not isinstance(model, GroupModel):
            for row in range(model.rowCount()):
                item_id = model.root_id(row)
                view.setExpanded(model.index(row, 0), item_id in expanded)
        idx = model.index_for_key(current)
        if idx.isValid():
            view.setCurrentIndex(idx)
        view.verticalScrollBar().setValue(scroll)
        view.viewport().update()
        if QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        self.status.setText(self._sort_previous_status.pop(view, self.status.text()))

    def _remember_column_width(self, view: QTreeView, column: int, width: int) -> None:
        if view in self._resizing_columns:
            return
        widths = self._column_base_widths.get(view)
        if widths is not None and 0 <= column < len(widths):
            widths[column] = width

    def _fill_columns(self, view: QTreeView) -> None:
        """Заповнити viewport за O(кількість колонок), не читаючи рядки.

        Базові ширини зберігаються, а вільне місце рівномірно додається до
        видимих колонок. Якщо вміст ширший за вікно, лишається горизонтальний
        скрол. Жодного resizeColumnToContents тут немає.
        """
        if view.model() is None:
            return
        visible = [c for c in range(view.model().columnCount())
                   if not view.isColumnHidden(c)]
        if not visible:
            return
        h = view.header()
        base = self._column_base_widths.get(view)
        if base is None or len(base) != view.model().columnCount():
            base = [h.sectionSize(c) for c in range(view.model().columnCount())]
            self._column_base_widths[view] = base
        leftover = max(0, view.viewport().width() - sum(base[c] for c in visible))
        share, extra = divmod(leftover, len(visible))
        self._resizing_columns.add(view)
        try:
            for c in visible:
                h.resizeSection(c, base[c] + share
                                + (extra if c == visible[0] else 0))
        finally:
            self._resizing_columns.discard(view)

    def _autofit(self, view: QTreeView) -> None:
        """Автоширина за header + видимими рядками, незалежно від dataset."""
        h = view.header()
        visible = [c for c in range(view.model().columnCount())
                   if not view.isColumnHidden(c)]
        metrics = view.fontMetrics()
        measured = {
            c: max(h.minimumSectionSize(), metrics.horizontalAdvance(
                str(view.model().headerData(c, Qt.Horizontal, Qt.DisplayRole))) + 32)
            for c in visible
        }
        idx = view.indexAt(view.viewport().rect().topLeft())
        visited = 0
        while idx.isValid() and visited < 1000:
            parent = idx.parent()
            for c in visible:
                cell = view.model().index(idx.row(), c, parent)
                shown = view.model().data(cell, Qt.DisplayRole)
                if shown is not None:
                    measured[c] = max(
                        measured[c], metrics.horizontalAdvance(str(shown)) + 36)
            nxt = view.indexBelow(idx)
            if not nxt.isValid() or nxt == idx:
                break
            idx = nxt
            visited += 1
        self._resizing_columns.add(view)
        try:
            for c in visible:
                h.resizeSection(c, min(measured[c], 900))
            widths = self._column_base_widths[view]
            for c in visible:
                widths[c] = h.sectionSize(c)
        finally:
            self._resizing_columns.discard(view)
        self._fill_columns(view)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "v_sim"):  # під час __init__ view ще нема
            # Усі 6 вкладок мусять бути тут — без будь-
            # якої з них ресайз вікна на пропущеній вкладці (currentIndex()
            # поза межами кортежу) кидав би IndexError.
            views = (
                self.v_files, self.v_dirs, self.v_sim, self.v_clusters,
                self.v_perceptual, self.v_problems)
            self._fill_columns(views[self.tabs.currentIndex()])

    def _header_menu(self, view: QTreeView, pos) -> None:
        m = QMenu(view)
        model = view.model()
        for c in range(model.columnCount()):
            name = model.headerData(c, Qt.Horizontal, Qt.DisplayRole)
            act = m.addAction(str(name))
            act.setCheckable(True)
            act.setChecked(not view.isColumnHidden(c))
            act.setEnabled(c != 0)  # шлях ховати не можна
            act.toggled.connect(
                lambda on, c=c, v=view: (v.setColumnHidden(c, not on),
                                         self._fill_columns(v)))
        m.addSeparator()
        m.addAction("Підігнати за видимими рядками", lambda v=view: self._autofit(v))
        m.exec(view.header().mapToGlobal(pos))

    def _expand_buttons(self, view: QTreeView, row: QHBoxLayout) -> None:
        b_ex = QPushButton("Розгорнути все")
        b_ex.clicked.connect(lambda: self._expand_all(view))
        b_co = QPushButton("Згорнути все")
        b_co.clicked.connect(lambda: self._set_all_expanded(view, False))
        row.addWidget(b_ex)
        row.addWidget(b_co)

    def _focus_search(self, model) -> None:
        edit = self._search_boxes.get(model)
        if edit is not None:
            edit.setFocus(Qt.ShortcutFocusReason)
            edit.selectAll()

    def _clear_search(self, model) -> None:
        edit = self._search_boxes.get(model)
        if edit is not None:
            edit.clear()

    def _set_view_density(self, view: QTreeView, compact: bool) -> None:
        density = scale_ui.validate_density(
            "compact" if compact else "comfortable")
        view.setProperty("dupscanDensity", density)
        if density == "compact":
            view.setStyleSheet(
                "QTreeView::item { min-height: 24px; padding: 1px 3px; }")
        else:
            view.setStyleSheet(
                "QTreeView::item { min-height: 34px; padding: 3px 5px; }")
        view.viewport().update()

    def _set_inspector_visible(self, model, visible: bool) -> None:
        panel = self._inspectors.get(model)
        split = self._splitters.get(model)
        if panel is None or split is None:
            return
        sizes = split.sizes()
        if not visible:
            if len(sizes) > 1 and sizes[1] > 0:
                self._inspector_last_size[model] = sizes[1]
            panel.hide()
            split.setSizes([max(1, sum(sizes)), 0])
            return
        panel.show()
        total = max(sum(sizes), split.width(), 900)
        detail = min(
            max(230, self._inspector_last_size.get(model, 260)),
            max(230, total // 2),
        )
        split.setSizes([max(1, total - detail), detail])

    def _toggle_active_inspector(self) -> None:
        model, _view = self._active_model_view()
        button = self._inspector_buttons.get(model)
        if button is not None:
            button.toggle()

    def _current_group_id(self, model: GroupModel, view: QTreeView) -> int | None:
        current = view.currentIndex()
        if not current.isValid():
            return None
        if current.internalId() == 0:
            return model.root_id(current.row())
        group_id = int(current.internalId()) - 1
        return group_id if 0 <= group_id < len(model.groups) else None

    def _announce_selection(self, model: GroupModel, scope_label: str) -> None:
        stats = model.selection_stats()
        self.status.setText(
            f"{scope_label}: позначено {stats['count']:,} у "
            f"{stats['groups']:,} групах · {human(stats['bytes'])}. "
            "Перед Кошиком буде review і свіжа перевірка.")

    def _select_group_scope(self, model: GroupModel, view: QTreeView) -> None:
        self._batch_selection_token += 1
        self._batch_selecting = False
        group_id = self._current_group_id(model, view)
        if group_id is None:
            self.status.setText(
                "Оберіть групу або її копію, тоді застосуйте пакетну дію.")
            return
        model.select_group_safe(group_id)
        self._announce_selection(model, "Поточна група")

    def _select_model_scope(self, model: GroupModel, scope: str) -> None:
        scope = scale_ui.validate_selection_scope(scope)
        if scope == "filtered":
            group_ids = list(model._group_order)
            count = sum(len(model._child_order[gi]) for gi in group_ids)
            label = "Поточний фільтр"
        elif scope == "all_safe":
            group_ids = list(range(len(model.groups)))
            count = sum(len(group.paths) for group in model.groups)
            label = "Усі результати"
        else:
            return
        if count <= 20_000:
            if scope == "filtered":
                model.select_filtered_safe()
            else:
                model.select_all_safe()
            self._announce_selection(model, label)
            return

        self._batch_selection_token += 1
        token = self._batch_selection_token
        self._batch_selecting = True
        if scope == "all_safe" and model.checked:
            model.checked.clear()
            model._emit_checks_changed()
            model.checksChanged.emit()
        position = 0

        def batch() -> None:
            nonlocal position
            if token != self._batch_selection_token or self._closing:
                return
            deadline = time.perf_counter() + 0.008
            scoped: dict[int, list[str]] = {}
            while position < len(group_ids) and time.perf_counter() < deadline:
                gi = group_ids[position]
                if scope == "filtered":
                    scoped[gi] = [
                        model.groups[gi].paths[ri]
                        for ri in model._child_order[gi]
                    ]
                else:
                    scoped[gi] = list(model.groups[gi].paths)
                position += 1
            model._select_scope_safe(scoped)
            if position < len(group_ids):
                self.status.setText(
                    f"{label}: оброблено груп {position:,} / "
                    f"{len(group_ids):,}…")
                QTimer.singleShot(0, batch)
                return
            self._batch_selecting = False
            self._update_selection_summary()
            self._announce_selection(model, label)

        QTimer.singleShot(0, batch)

    def _select_active_filtered(self) -> None:
        model, _view = self._active_model_view()
        if isinstance(model, GroupModel):
            self._select_model_scope(model, "filtered")
        else:
            self.status.setText(
                "Для подібних папок рішення приймаються по конкретній парі.")

    def _clear_model_scope(self, model: GroupModel, *, filtered: bool) -> None:
        self._batch_selection_token += 1
        self._batch_selecting = False
        if filtered:
            model.clear_filtered()
            label = "Знято позначки лише у поточному фільтрі"
        else:
            model.clear_checks()
            label = "Знято всі позначки на вкладці"
        stats = model.selection_stats()
        self.status.setText(
            f"{label}. Залишилось: {stats['count']:,} · {human(stats['bytes'])}.")

    def _apply_view_preset(self, model: GroupModel, preset: str) -> None:
        if preset not in scale_ui.PRESETS:
            preset = "all"
        model.set_view_preset(preset)
        label = (
            "Зовнішні / мережеві джерела"
            if preset == "network" else scale_ui.PRESETS[preset].label
        )
        self.status.setText(f"Представлення: {label}. Позначки збережено.")

    def _expand_all(self, view: QTreeView) -> None:
        total = cast(_TreeModel, view.model()).total_children()
        if total > 10_000:
            answer = QMessageBox.question(
                self, "Розгорнути великий список?",
                f"У списку {total:,} вкладених рядків. Вони завантажуються "
                "порціями, але повне розгортання може зайняти час. Продовжити?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._set_all_expanded(view, True)

    def _set_all_expanded(self, view: QTreeView, on: bool) -> None:
        """Розгортати/згортати дерево короткими UI-квантами, не одним freeze."""
        model = cast(_TreeModel, view.model())
        if on:
            item_ids = [model.root_id(row) for row in range(model.rowCount())]
        elif isinstance(model, GroupModel):
            item_ids = list(model._expanded)
        else:
            item_ids = [
                model.root_id(row) for row in range(model.rowCount())
                if view.isExpanded(model.index(row, 0))
            ]
        # Окрема назва (live_ids) для звуженого list[int] —
        # mypy не звужує елемент list[int | None] за фільтром "is not None"
        # у comprehension; item_ids лишався б int | None при індексуванні.
        live_ids: list[int] = [item_id for item_id in item_ids if item_id is not None]
        token = self._tree_bulk_token.get(view, 0) + 1
        self._tree_bulk_token[view] = token
        self._tree_bulk_status.setdefault(view, self.status.text())
        total = len(live_ids)
        done = 0

        def batch() -> None:
            nonlocal done
            if self._tree_bulk_token.get(view) != token:
                return
            deadline = time.perf_counter() + 0.008
            while done < total and time.perf_counter() < deadline:
                item_id = live_ids[done]
                row = model.root_row(item_id)
                if row is not None:
                    view.setExpanded(model.index(row, 0), on)
                done += 1
            if done < total:
                action = "Розгортаю" if on else "Згортаю"
                self.status.setText(f"{action} групи: {done} / {total}")
                QTimer.singleShot(0, batch)
                return
            self.status.setText(self._tree_bulk_status.pop(view, self.status.text()))
            view.viewport().update()

        QTimer.singleShot(0, batch)

    def _plain_menu_button(
            self, text: str, accessible_name: str) -> tuple[QPushButton, QMenu]:
        """Use an ellipsis as the menu affordance, without a drawn chevron."""
        button = QPushButton(text)
        button.setProperty("plainMenuTrigger", True)
        button.setAccessibleName(accessible_name)
        menu = QMenu(button)
        button.clicked.connect(
            lambda _checked=False, control=button, popup=menu:
            popup.exec(control.mapToGlobal(control.rect().bottomLeft())))
        return button, menu

    def _dup_tab(self, view, model, *, categories: bool = False) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self._search_box(model), 1)

        preset = QComboBox()
        preset.setAccessibleName("Збережене представлення результатів")
        for key, definition in scale_ui.PRESETS.items():
            label = (
                "Зовнішні / мережеві джерела"
                if key == "network" else definition.label
            )
            preset.addItem(label, key)
        name = self._view_names[view]
        saved_preset = str(self._settings.value(
            f"views/{name}/preset", "all"))
        if saved_preset not in scale_ui.PRESETS:
            saved_preset = "all"
        preset.setCurrentIndex(max(0, preset.findData(saved_preset)))
        preset.currentIndexChanged.connect(
            lambda _i, m=model, box=preset:
            self._apply_view_preset(m, box.currentData()))
        self._preset_boxes[model] = preset
        toolbar.addWidget(preset)

        if categories:
            category = QComboBox()
            for key, label in product.CATEGORY_LABELS.items():
                category.addItem(label, key)
            category.setAccessibleName("Фільтр категорії")
            saved_category = str(self._settings.value(
                f"views/{name}/category", "all"))
            if saved_category not in product.CATEGORY_LABELS:
                saved_category = "all"
            category.setCurrentIndex(max(0, category.findData(saved_category)))
            category.currentIndexChanged.connect(
                lambda _i, m=model, box=category:
                m.set_category(box.currentData()))
            self._category_boxes[model] = category
            toolbar.addWidget(category)

        select_button, select_menu = self._plain_menu_button(
            "Позначення…", "Безпечне позначення кандидатів до Кошика")
        select_button.setObjectName(f"{name}SelectionMenuButton")
        select_button.setMinimumWidth(125)
        select_menu.addAction(
            "Позначити поточну групу безпечно",
            lambda m=model, item_view=view:
            self._select_group_scope(m, item_view))
        select_menu.addAction(
            "Позначити весь поточний фільтр",
            lambda m=model: self._select_model_scope(m, "filtered"))
        select_menu.addAction(
            "Позначити всі результати безпечно",
            lambda m=model: self._select_model_scope(m, "all_safe"))
        select_menu.addSeparator()
        select_menu.addAction(
            "Зняти позначки у поточному фільтрі",
            lambda m=model: self._clear_model_scope(m, filtered=True))
        select_menu.addAction(
            "Зняти всі позначки",
            lambda m=model: self._clear_model_scope(m, filtered=False))
        select_menu.addSeparator()
        select_menu.addAction("Теки-еталони…", self._manage_reference_roots)
        self._selection_menu_buttons[model] = select_button
        toolbar.addWidget(select_button)

        view_button, view_menu = self._plain_menu_button(
            "Вигляд…", "Налаштувати вигляд поточної таблиці")
        view_button.setObjectName(f"{name}ViewMenuButton")

        density = QAction("Компактні рядки", view_button)
        density.setCheckable(True)
        saved_density = str(self._settings.value(
            f"views/{name}/density", "compact"))
        try:
            saved_density = scale_ui.validate_density(saved_density)
        except ValueError:
            saved_density = "compact"
        density.setChecked(saved_density == "compact")
        density.toggled.connect(
            lambda on, item_view=view: self._set_view_density(item_view, on))
        self._density_buttons[model] = density
        view_menu.addAction(density)

        inspector = QAction("Показати деталі", view_button)
        inspector.setCheckable(True)
        raw_visible = str(self._settings.value(
            f"views/{name}/inspector_visible", "false")).lower()
        inspector.setChecked(raw_visible in {"1", "true", "yes"})
        inspector.toggled.connect(
            lambda on, m=model: self._set_inspector_visible(m, on))
        self._inspector_buttons[model] = inspector
        view_menu.addAction(inspector)
        view_menu.addSeparator()
        view_menu.addAction(
            "Розгорнути групи порціями",
            lambda item_view=view: self._expand_all(item_view))
        view_menu.addAction(
            "Згорнути всі групи",
            lambda item_view=view: self._set_all_expanded(item_view, False))
        self._view_menu_buttons[model] = view_button
        toolbar.addWidget(view_button)
        v.addLayout(toolbar)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(view)
        panel = self._details_panel(view, model)
        split.addWidget(panel)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        self._splitters[model] = split
        self._inspectors[model] = panel
        raw_size = self._settings.value(f"views/{name}/inspector_size", 260)
        try:
            # Той самий QSettings.value()->object->int() випадок.
            self._inspector_last_size[model] = max(
                230, int(raw_size))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            self._inspector_last_size[model] = 260
        v.addWidget(split, 1)
        model.set_view_preset(saved_preset)
        model.set_category(
            self._category_boxes[model].currentData()
            if model in self._category_boxes else "all")
        self._set_view_density(view, density.isChecked())
        self._set_inspector_visible(model, inspector.isChecked())
        return w

    def _sim_tab(self, view) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self._search_box(self.m_sim), 1)
        toolbar.addWidget(QLabel("Подібність від:"))
        threshold = QSpinBox()
        threshold.setRange(0, 100)
        threshold.setSuffix(" %")
        threshold.setValue(10)
        threshold.setAccessibleName("Мінімальна подібність папок")
        threshold.valueChanged.connect(self.m_sim.set_threshold)
        self.m_sim.set_threshold(threshold.value())
        toolbar.addWidget(threshold)

        self.b_clear_similarity_marks = QPushButton("Зняти позначки")
        self.b_clear_similarity_marks.setObjectName(
            "similarityClearMarksButton")
        self.b_clear_similarity_marks.setAccessibleName(
            "Зняти всі позначки подібних папок")
        self.b_clear_similarity_marks.clicked.connect(
            lambda: (self.m_sim.clear_checks(), view.viewport().update()))
        toolbar.addWidget(self.b_clear_similarity_marks)
        self.b_ignored = QPushButton("Скинути ігнорування")
        self.b_ignored.setVisible(False)
        self.b_ignored.clicked.connect(self._sim_reset_ignored)
        toolbar.addWidget(self.b_ignored)

        view_button, view_menu = self._plain_menu_button(
            "Вигляд…", "Налаштувати вигляд подібних папок")
        view_button.setObjectName("similarityViewMenuButton")
        density = QAction("Компактні рядки", view_button)
        density.setCheckable(True)
        name = self._view_names[view]
        saved_density = str(self._settings.value(
            f"views/{name}/density", "compact"))
        try:
            saved_density = scale_ui.validate_density(saved_density)
        except ValueError:
            saved_density = "compact"
        density.setChecked(saved_density == "compact")
        density.toggled.connect(
            lambda on, item_view=view: self._set_view_density(item_view, on))
        self._density_buttons[self.m_sim] = density
        view_menu.addAction(density)
        inspector = QAction("Показати деталі", view_button)
        inspector.setCheckable(True)
        inspector.setChecked(str(self._settings.value(
            f"views/{name}/inspector_visible", "false")).lower()
            in {"1", "true", "yes"})
        inspector.toggled.connect(
            lambda on: self._set_inspector_visible(self.m_sim, on))
        self._inspector_buttons[self.m_sim] = inspector
        view_menu.addAction(inspector)
        view_menu.addSeparator()
        view_menu.addAction(
            "Розгорнути групи порціями",
            lambda: self._expand_all(view))
        view_menu.addAction(
            "Згорнути всі групи",
            lambda: self._set_all_expanded(view, False))
        self._view_menu_buttons[self.m_sim] = view_button
        toolbar.addWidget(view_button)
        v.addLayout(toolbar)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(view)
        panel = self._details_panel(view, self.m_sim)
        split.addWidget(panel)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        self._splitters[self.m_sim] = split
        self._inspectors[self.m_sim] = panel
        raw_size = self._settings.value(f"views/{name}/inspector_size", 260)
        try:
            # Той самий QSettings.value()->object->int() випадок.
            self._inspector_last_size[self.m_sim] = max(
                230, int(raw_size))  # type: ignore[call-overload]
        except (TypeError, ValueError):
            self._inspector_last_size[self.m_sim] = 260
        v.addWidget(split, 1)
        self._set_view_density(view, density.isChecked())
        self._set_inspector_visible(self.m_sim, inspector.isChecked())
        return w

    # ---- Кластери тек: побудова й обробники — ui/clusters_tab_controller.py --
    def _reveal_cluster_dir(self, idx) -> None:
        self._clusters_ctl.reveal_dir(idx)

    def _clusters_menu(self, pos) -> None:
        self._clusters_ctl.menu(pos)

    def _find_pair_for_cluster_dir(self, path: str) -> None:
        self._clusters_ctl.find_pair_for_cluster_dir(path)

    # ---- «Схожі фото (підказка)»: побудова й обробники —
    # ui/perceptual_tab_controller.py; worker/_perceptual_scanned_for
    # лишаються станом Main (на них зав'язаний _finish_model_refresh). ------
    def _perceptual_scan(self) -> None:
        self._perceptual_ctl.scan()

    def _perceptual_progress(self, phase: str, done: int, total: int) -> None:
        self._perceptual_ctl.progress(phase, done, total)

    def _perceptual_pause_toggle(self, checked: bool) -> None:
        self._perceptual_ctl.pause_toggle(checked)

    def _perceptual_cancel(self) -> None:
        self._perceptual_ctl.cancel()

    def _perceptual_reset_controls(self) -> None:
        self._perceptual_ctl.reset_controls()

    def _perceptual_done(self, result: perceptual.PerceptualResult) -> None:
        self._perceptual_ctl.done(result)

    def _perceptual_failed(self, message: str) -> None:
        self._perceptual_ctl.failed(message)

    def _perceptual_quick_look(self, idx) -> None:
        self._perceptual_ctl.quick_look(idx)

    def _perceptual_menu(self, pos) -> None:
        self._perceptual_ctl.menu(pos)

    # ---- Вкладка «Проблеми»: побудова й обробники —
    # ui/problems_tab_controller.py ------------------------------------
    def _problems_menu(self, pos) -> None:
        self._problems_ctl.menu(pos)

    def _problems_fix_name_at_path(self, path: str) -> None:
        self._problems_ctl.fix_name_at_path(path)

    def _update_problems_namefix_button(self) -> None:
        self._problems_ctl.update_namefix_button()

    def _problems_namefix_bulk(self) -> None:
        self._problems_ctl.namefix_bulk()

    def _details_panel(self, view: QTreeView, model) -> QWidget:
        panel = QFrame()
        view_name = self._view_names.get(view, "results")
        _accessible(
            panel, f"{view_name}Inspector", "Деталі вибраного результату",
            "Повні шляхи, розмір, статус перевірки й безпечні дії.")
        panel.setFrameShape(QFrame.StyledPanel)
        panel.setMinimumWidth(230)
        layout = QVBoxLayout(panel)
        title = QLabel("Деталі")
        title.setStyleSheet("font-weight: 600; font-size: 14px;")
        text = QLabel("Оберіть групу або файл у таблиці.")
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        layout.addWidget(title)
        layout.addWidget(text, 1)

        def update(idx, _previous=QModelIndex()):
            if not idx.isValid():
                text.setText("Оберіть групу або файл у таблиці.")
                return
            if isinstance(model, GroupModel):
                if idx.internalId() == 0:
                    gi = model.root_id(idx.row())
                    if gi is None:
                        text.setText(
                            "Активуйте рядок «Показати ще», щоб відкрити "
                            "наступну сторінку груп.")
                        return
                    group = model.groups[gi]
                    text.setText(
                        f"Копій: {len(group.paths)}\n"
                        f"Розмір копії: {human(group.size)}\n"
                        f"Потенційно звільниться: {human(group.wasted)}\n\n"
                        "🛡 Перед Кошиком обидві копії будуть повністю перевірені.")
                else:
                    path = model._path(idx)
                    text.setText(
                        f"{path}\n\nПозначено для Кошика: "
                        f"{'так' if path in model.checked else 'ні'}")
            else:
                pi = (model.root_id(idx.row()) if idx.internalId() == 0
                      else int(idx.internalId()) - 1)
                if pi is None:
                    text.setText(
                        "Активуйте рядок «Показати ще», щоб відкрити "
                        "наступну сторінку подібних папок.")
                    return
                pair = model.pairs[pi]
                tag = model.tags.get(model.pair_key(pair), "—")
                text.setText(
                    f"Тека A:\n{pair.dir_a}\n\nТека B:\n{pair.dir_b}\n\n"
                    f"Подібність: {pair.percent:.1f} %\n"
                    f"Спільні дані: {human(pair.shared_bytes)}\n\n"
                    f"Мітка: {tag}\n\n"
                    "Як розраховано: подвоєний обсяг спільного вмісту, "
                    "поділений на сумарний обсяг обох тек. Назви файлів "
                    "не впливають — порівнюється підтверджений вміст.")

        view.selectionModel().currentChanged.connect(update)
        model.checksChanged.connect(lambda: update(view.currentIndex()))
        return panel

    def _search_box(self, model) -> QLineEdit:
        edit = SearchLineEdit()
        edit.setClearButtonEnabled(True)
        edit.setPlaceholderText("Пошук у шляхах…")
        edit.setAccessibleName("Пошук у результатах")
        view = self._model_views.get(model)
        # _search_box може відпрацювати до реєстрації моделі
        # у _model_views (view лишається None) — раніше поведінка та сама
        # (dict.get(None, ...) з ключем-не-QTreeView просто не знаходив
        # нічого і падав на дефолт), тепер це явно типобезпечно.
        name = self._view_names.get(view, "results") if view is not None else "results"
        saved = str(self._settings.value(f"views/{name}/query", ""))
        if len(saved) > 4096:
            saved = ""
        edit.setText(saved)
        timer = QTimer(edit)
        timer.setSingleShot(True)
        timer.setInterval(180)
        timer.timeout.connect(lambda: self._filter_model(model, edit))
        edit.textChanged.connect(lambda _text: timer.start())
        edit.escapePressed.connect(edit.clear)
        self._search_boxes[model] = edit
        return edit

    def _filter_model(self, model, edit: QLineEdit) -> None:
        text = edit.text()
        if not isinstance(model, GroupModel) or model.total_children() < 20_000:
            if isinstance(model, GroupModel):
                state = self._filter_view_state(model)
                model.set_filter(text)
                self._restore_filter_view(model, state, model._filter_serial)
            else:
                model.set_filter(text)
            return
        previous = self._filter_cancel.get(model)
        if previous is not None:
            previous.set()
        cancel = threading.Event()
        self._filter_cancel[model] = cancel
        serial, job = model.prepare_filter(text, cancel)
        if model not in self._filter_status:
            self._filter_status[model] = self.status.text()
        self.status.setText("Фільтрую великий список…")

        def ready(payload, model=model, edit=edit, serial=serial) -> None:
            if payload is None or serial != model._filter_serial:
                return
            view_state = self._filter_view_state(model)
            state = model.apply_filter_result(payload)
            if state == "stale":
                QTimer.singleShot(0, lambda: self._filter_model(model, edit))
                return
            if state != "applied":
                return
            self._filter_cancel.pop(model, None)
            self._restore_filter_view(model, view_state, serial)
            self.status.setText(self._filter_status.pop(model, self.status.text()))

        self._ui_bg_run(job, ready)

    def _filter_view_state(self, model: GroupModel):
        view = self._model_views[model]
        return (
            set(model._expanded),
            model.item_key(view.currentIndex()),
            view.verticalScrollBar().value(),
        )

    def _restore_filter_view(self, model: GroupModel, state, serial: int) -> None:
        """Повернути контекст після reset малими event-loop порціями."""
        view = self._model_views[model]
        expanded, current, scroll = state
        pending = [item_id for item_id in expanded if model.root_row(item_id) is not None]

        def restore_batch() -> None:
            if serial != model._filter_serial:
                return
            for _ in range(min(2, len(pending))):
                item_id = pending.pop(0)
                row = model.root_row(item_id)
                if row is not None:
                    view.setExpanded(model.index(row, 0), True)
            if pending:
                QTimer.singleShot(0, restore_batch)
                return
            idx = model.index_for_key(current)
            if idx.isValid():
                view.setCurrentIndex(idx)
            view.verticalScrollBar().setValue(scroll)
            view.viewport().update()

        QTimer.singleShot(0, restore_batch)

    def _reveal_idx(self, pidx) -> None:
        m = pidx.model()
        p = m.path_at(pidx) if isinstance(m, SimModel) else m._path(pidx)
        if p:
            self._open_result_path(p, reveal)

    def _session_path_candidate(self, path: str) -> str:
        """Apply only the current in-memory relink; never mutate a session."""
        normalized = os.path.normpath(os.path.abspath(path))
        res = self.result
        if res is None or res.live:
            return normalized
        root = containing_session_root(normalized, self._loaded_session_roots)
        mapped = self._session_root_map.get(root) if root else None
        return (
            rebase_session_path(normalized, root, mapped)
            if root is not None and mapped is not None else normalized
        )

    def _warn_unavailable_session_path(
            self, original: str, candidate: str, detail: str = "") -> None:
        message = (
            "Елемент із результатів сканування більше не існує за збереженим "
            "шляхом.\n\n"
            f"Шлях у сесії:\n{original}"
        )
        if candidate != original:
            message += f"\n\nПеревірений поточний шлях:\n{candidate}"
        if detail:
            message += f"\n\nПричина:\n{detail}"
        QMessageBox.warning(self, "Шлях сесії недоступний", message)
        self.status.setText("Шлях сесії недоступний; файли не змінювались.")

    def _resolve_session_paths(
            self, paths: list[str] | tuple[str, ...], *,
            purpose: str, on_ready, expect_directories: bool = False,
            _context_token: int | None = None) -> None:
        """Resolve current paths asynchronously, offering one explicit relink.

        A relink is runtime-only. It maps the longest containing root from the
        loaded session to one user-selected current directory and validates
        every path required by this action before storing the mapping.
        """
        originals = tuple(
            os.path.normpath(os.path.abspath(path)) for path in paths
        )
        if not originals:
            return
        context_token = (
            self._session_context_token
            if _context_token is None else _context_token
        )
        if context_token != self._session_context_token:
            return
        candidates = tuple(
            self._session_path_candidate(path) for path in originals)

        def check_current():
            probe = os.path.isdir if expect_directories else os.path.exists
            return candidates, tuple(probe(path) for path in candidates)

        def checked(value) -> None:
            if context_token != self._session_context_token:
                return
            candidates, available = value
            if all(available):
                on_ready(candidates)
                return
            missing_index = available.index(False)
            original = originals[missing_index]
            candidate = candidates[missing_index]
            res = self.result
            old_root = containing_session_root(
                original, self._loaded_session_roots)
            if res is None or res.live or old_root is None:
                self._warn_unavailable_session_path(original, candidate)
                return
            hint = self._session_relink_hints.get(old_root)
            if hint and old_root not in self._session_root_map:
                self._try_session_relink_hint(
                    old_root, hint, originals, purpose=purpose,
                    on_ready=on_ready,
                    expect_directories=expect_directories,
                    context_token=context_token,
                )
                return
            self._offer_session_relink(
                old_root, originals, purpose=purpose, on_ready=on_ready,
                expect_directories=expect_directories,
                context_token=context_token,
            )

        def failed(error: str) -> None:
            if context_token != self._session_context_token:
                return
            self._warn_unavailable_session_path(
                originals[0], self._session_path_candidate(originals[0]), error)

        self._ui_bg_run(
            check_current, checked, failed)

    def _try_session_relink_hint(
            self, old_root: str, hinted_root: str,
            requested_paths: tuple[str, ...], *, purpose: str, on_ready,
            expect_directories: bool, context_token: int) -> None:
        """Revalidate a prior user choice before reusing it for a new session."""
        if context_token != self._session_context_token:
            return
        affected = tuple(
            path for path in requested_paths
            if containing_session_root(path, self._loaded_session_roots) == old_root
        )

        def validate():
            translated = tuple(
                rebase_session_path(path, old_root, hinted_root)
                for path in affected
            )
            probe = os.path.isdir if expect_directories else os.path.exists
            return (
                os.path.isdir(hinted_root),
                os.path.islink(hinted_root),
                translated,
                tuple(probe(path) for path in translated),
            )

        def validated(value) -> None:
            if context_token != self._session_context_token:
                return
            is_directory, is_link, _translated, available = value
            if is_directory and not is_link and affected and all(available):
                self._session_root_map[old_root] = hinted_root
                self.status.setText(
                    f"Перевірено попереднє переприв’язування: "
                    f"{old_root} → {hinted_root}")
                self._resolve_session_paths(
                    requested_paths, purpose=purpose, on_ready=on_ready,
                    expect_directories=expect_directories,
                    _context_token=context_token,
                )
                return
            if self._session_relink_hints.get(old_root) == hinted_root:
                self._session_relink_hints.pop(old_root, None)
            self._offer_session_relink(
                old_root, requested_paths, purpose=purpose,
                on_ready=on_ready, expect_directories=expect_directories,
                context_token=context_token,
            )

        def failed(_error: str) -> None:
            if context_token != self._session_context_token:
                return
            if self._session_relink_hints.get(old_root) == hinted_root:
                self._session_relink_hints.pop(old_root, None)
            self._offer_session_relink(
                old_root, requested_paths, purpose=purpose,
                on_ready=on_ready, expect_directories=expect_directories,
                context_token=context_token,
            )

        self._ui_bg_run(validate, validated, failed)

    def _offer_session_relink(
            self, old_root: str, requested_paths: tuple[str, ...], *,
            purpose: str, on_ready, expect_directories: bool,
            context_token: int) -> None:
        if context_token != self._session_context_token:
            return
        if self._session_relinking:
            QMessageBox.information(
                self, "DupScan",
                "Дочекайтеся завершення поточного переприв’язування сесії.")
            return
        self._session_relinking = True
        answer = QMessageBox.question(
            self, "Знайти переміщений диск або кореневу теку?",
            "Дані історичної сесії переміщено або цей том змонтовано з іншою "
            "структурою. DupScan може тимчасово переприв’язати старий корінь "
            "до його поточного розташування.\n\n"
            f"Старий корінь сесії:\n{old_root}\n\n"
            f"Потрібно для дії:\n{purpose}\n\n"
            "Сесія на диску не зміниться, повного повторного сканування не "
            "буде. Обрати поточну кореневу теку?")
        if answer != QMessageBox.StandardButton.Yes:
            self._session_relinking = False
            self.status.setText("Переприв’язування сесії скасовано.")
            return
        selected = list(dict.fromkeys(pick_dirs(
            self,
            title=f"Де зараз знаходиться {old_root}?",
            accept_label="Переприв’язати корінь",
        )))
        if len(selected) != 1:
            self._session_relinking = False
            if selected:
                QMessageBox.information(
                    self, "Потрібна одна тека",
                    "Оберіть рівно одну поточну кореневу теку для цього "
                    "кореня історичної сесії.")
            return
        new_root = os.path.normpath(os.path.abspath(selected[0]))
        affected = tuple(
            path for path in requested_paths
            if containing_session_root(path, self._loaded_session_roots) == old_root
        )

        def validate():
            translated = tuple(
                rebase_session_path(path, old_root, new_root)
                for path in affected
            )
            probe = os.path.isdir if expect_directories else os.path.exists
            return (
                os.path.isdir(new_root),
                os.path.islink(new_root),
                translated,
                tuple(probe(path) for path in translated),
            )

        def validated(value) -> None:
            if context_token != self._session_context_token:
                return
            is_directory, is_link, translated, available = value
            self._session_relinking = False
            if not is_directory or is_link or not affected or not all(available):
                candidate = (
                    translated[available.index(False)]
                    if translated and False in available else new_root
                )
                original = (
                    affected[available.index(False)]
                    if affected and False in available else old_root
                )
                detail = (
                    "Новий корінь має бути реальною текою, не символічним "
                    "посиланням, і містити потрібний елемент у тому самому "
                    "відносному розташуванні.")
                self._warn_unavailable_session_path(original, candidate, detail)
                return
            self._session_root_map[old_root] = new_root
            self._session_relink_hints[old_root] = new_root
            self.status.setText(
                f"Сесію тимчасово переприв’язано: {old_root} → {new_root}")
            self._resolve_session_paths(
                requested_paths, purpose=purpose, on_ready=on_ready,
                expect_directories=expect_directories,
                _context_token=context_token,
            )

        def failed(error: str) -> None:
            if context_token != self._session_context_token:
                return
            self._session_relinking = False
            self._warn_unavailable_session_path(old_root, new_root, error)

        self._ui_bg_run(validate, validated, failed)

    def _launch_result_path(self, path: str, opener) -> None:
        self.status.setText(f"Відкриваю: {path}")
        self._ui_bg_run(
            lambda: opener(path),
            lambda _value: self.status.setText(f"Відкрито: {path}"),
            lambda error: self._warn_unavailable_session_path(
                path, path, f"macOS не змогла відкрити елемент: {error}"),
        )

    def _open_result_path(self, path: str, opener) -> None:
        name = "Quick Look" if opener is quick_look else "Показати у Finder"
        self._resolve_session_paths(
            (path,), purpose=name,
            on_ready=lambda values: self._launch_result_path(values[0], opener),
        )

    def _session_refresh_busy(self) -> bool:
        """Operations that refresh may not supersede.

        Presentation-only model preparation is intentionally absent: refresh
        cancels that work through its token/cancel barrier.
        """
        return bool(
            self._loading or self._trashing or self._merging
            or self._refresh_preflighting or self._session_relinking
            or self._batch_selecting
            or (self._rw and self._rw.isRunning())
            or (self.worker and self.worker.isRunning())
            or (self.refresh_worker and self.refresh_worker.isRunning())
            or (self.pair_worker and self.pair_worker.isRunning())
            or (self.merge_worker and self.merge_worker.isRunning())
            or any(worker.isRunning() for worker in self._bg)
        )

    def _cancel_model_preparation_for_replacement(self) -> None:
        if not self._model_preparing:
            return
        self._model_prepare_token += 1
        if self._model_prepare_cancel is not None:
            self._model_prepare_cancel.set()
        self._model_preparing = False
        self._model_prepare_cancel = None
        self.tabs.setEnabled(True)

    def _update_refresh_session_button(self, result=None) -> None:
        result = self.result if result is None else result
        available = bool(result and (not result.live or result.partial))
        self.session_rescan_panel.setVisible(available)
        self.b_refresh_session.setEnabled(
            available and not self._session_refresh_busy())

    def _session_refresh_confirmation_box(
            self, historical_roots: tuple[str, ...]
            ) -> tuple[QMessageBox, QPushButton]:
        roots_text = "\n".join(historical_roots)
        box = QMessageBox(self)
        box.setWindowTitle("Оновити історичну сесію")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "DupScan перевірить поточну структуру коренів цієї сесії та "
            "знайде додані, видалені й змінені файли.\n\n"
            f"Корені сесії:\n{roots_text}\n\n"
            "Буде виконано metadata-обхід. Незмінені повні BLAKE3 "
            "використовуються повторно лише після точного збігу файлової "
            "identity; нові й змінені кандидати читаються заново.\n\n"
            "Історичний snapshot не зміниться. Успіх створить нову "
            "актуальну сесію.")
        refresh = box.addButton(
            "Оновити дані", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(
            "Залишити історичний перегляд",
            QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(refresh)
        box.setEscapeButton(cancel)
        return box, refresh

    def _confirm_session_refresh(
            self, historical_roots: tuple[str, ...]) -> bool:
        box, refresh = self._session_refresh_confirmation_box(historical_roots)
        box.exec()
        return box.clickedButton() is refresh

    def refresh_loaded_session(self) -> None:
        """Offer one authoritative background refresh of current roots."""
        result = self.result
        if result is None or (result.live and not result.partial):
            return
        if self._session_refresh_busy():
            QMessageBox.information(
                self, "DupScan", "Зачекайте завершення поточної операції.")
            return
        historical_roots = (
            self._loaded_session_roots
            if self._loaded_session_roots else tuple(self._last_roots)
        )
        if not historical_roots:
            QMessageBox.warning(
                self, "Немає коренів сесії",
                "DupScan не знає, які корені треба оновити. Запустіть новий "
                "пошук і явно оберіть папки або диски.")
            return
        if not self._confirm_session_refresh(tuple(historical_roots)):
            return
        self._begin_loaded_session_refresh(
            result, tuple(historical_roots))

    def _begin_loaded_session_refresh(
            self, result: core.ScanResult,
            historical_roots: tuple[str, ...]) -> None:
        """Move a parsed historical snapshot into one live refresh flow."""
        self._cancel_model_preparation_for_replacement()
        self._session_refresh_root_overrides.clear()
        if self._refresh_preflight_cancel is not None:
            self._refresh_preflight_cancel.set()
        self._refresh_preflight_cancel = threading.Event()
        self._refresh_preflighting = True
        self.b_refresh_session.setEnabled(False)
        self.b_scan.setEnabled(False)
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self._set_operation_controls_visible(True)
        self.nav_compare.setEnabled(False)
        self.status.setText("Перевіряю поточні корені історичної сесії…")
        self._preflight_session_refresh(
            result, tuple(historical_roots), self._session_context_token)

    def _show_session_refresh_placeholder(self) -> None:
        """Remove rows from a previous session while current data is pending."""
        self.m_files.set_groups([])
        self.m_dirs.set_groups([])
        self.m_sim.set_pairs([])
        self.summary.setText(
            "АКТУАЛІЗАЦІЯ — історичний знімок прочитано. "
            "Поточні шляхи та дії з’являться після успішної перевірки.")
        self.tabs.setTabText(0, "Файли-дублікати")
        self.tabs.setTabText(1, "Папки-дублікати")
        self.tabs.setTabText(2, "Подібність папок")
        self._update_selection_summary()
        self._update_action_state()

    def _restore_deferred_historical_models(self) -> None:
        if not self._refresh_models_deferred:
            return
        self._refresh_models_deferred = False
        self._refresh_models()

    def _cancel_session_refresh_preflight(self, note: str) -> None:
        if self._refresh_preflight_cancel is not None:
            self._refresh_preflight_cancel.set()
        self._refresh_preflight_cancel = None
        self._refresh_preflighting = False
        self._session_refresh_root_overrides.clear()
        self.b_scan.setEnabled(True)
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_operation_controls_visible(False)
        self.b_refresh_session.setEnabled(True)
        self.nav_compare.setEnabled(True)
        self._restore_deferred_historical_models()
        self.status.setText(note)

    def _preflight_session_refresh(
            self, previous: core.ScanResult, historical_roots: tuple[str, ...],
            context_token: int) -> None:
        """Resolve root health without probing disk in GUI thread."""
        refresh_overrides = dict(self._session_refresh_root_overrides)
        if self._refresh_preflight_cancel is None:
            self._refresh_preflight_cancel = threading.Event()
        cancel_event = self._refresh_preflight_cancel

        def check():
            samples = session_path_samples(previous, historical_roots)
            states = []
            for old_root in historical_roots:
                if cancel_event.is_set():
                    return None
                # Never derive full-refresh scope from a selective A/B map.
                # Only the root explicitly selected by the refresh picker may
                # replace a declared root that still exists as an empty parent
                # mount point (for example L -> L/BARRACUDA).
                current_root = refresh_overrides.get(old_root, old_root)
                translated = tuple(
                    rebase_session_path(path, old_root, current_root)
                    for path in samples.get(old_root, ())
                )
                root_samples = samples.get(old_root, ())
                specs = session_probe_specs(previous, root_samples)
                is_directory = _real_directory(current_root)
                is_link = os.path.islink(current_root)
                hits = sum(
                    _session_probe_matches(
                        path,
                        specs.get(os.path.normpath(os.path.abspath(old_path))),
                    )
                    for old_path, path in zip(
                        root_samples, translated, strict=True)
                )
                auto_root = None
                if (
                    current_root == old_root
                    and is_directory
                    and not is_link
                    and samples.get(old_root, ())
                    and hits == 0
                ):
                    auto_root = detect_nested_session_root(
                        old_root, root_samples, probe_specs=specs,
                        cancel=cancel_event)
                states.append((
                    old_root, current_root, root_samples,
                    is_directory, is_link, hits, auto_root, specs,
                ))
            return states

        def checked(states) -> None:
            if (
                states is None
                or cancel_event.is_set()
                or cancel_event is not self._refresh_preflight_cancel
                or context_token != self._session_context_token
                or previous is not self.result
                or not self._refresh_preflighting
            ):
                return
            unresolved = next((
                state for state in states
                if not state[3] or state[4] or (state[2] and state[5] == 0)
            ), None)
            if unresolved is not None:
                auto_root = unresolved[6]
                if auto_root is not None:
                    old_root = unresolved[0]
                    new_root, hits, total = auto_root
                    self._session_refresh_root_overrides[old_root] = new_root
                    self.status.setText(
                        "Автоматично знайдено поточний корінь: "
                        f"{old_root} → {new_root} "
                        f"({hits} із {total} контрольних шляхів).")
                    self._preflight_session_refresh(
                        previous, historical_roots, context_token)
                    return
                self._offer_session_refresh_root(
                    previous, historical_roots, context_token, unresolved)
                return
            current_roots = list(dict.fromkeys(state[1] for state in states))
            self._start_session_refresh(
                previous, historical_roots, current_roots, context_token)

        def failed(error: str) -> None:
            if (
                cancel_event.is_set()
                or cancel_event is not self._refresh_preflight_cancel
                or context_token != self._session_context_token
            ):
                return
            self._cancel_session_refresh_preflight(
                f"Не вдалося перевірити корені сесії: {error}")

        self._ui_bg_run(check, checked, failed)

    def _refresh_root_choice_box(
            self, old_root: str, current_root: str
            ) -> tuple[QMessageBox, QPushButton]:
        old_normalized = os.path.normcase(
            os.path.normpath(os.path.abspath(old_root)))
        current_normalized = os.path.normcase(
            os.path.normpath(os.path.abspath(current_root)))
        if old_normalized == current_normalized:
            roots_text = (
                f"Збережений корінь:\n{old_root}\n\n"
                "За цим шляхом не знайдено контрольних файлів сесії.")
        else:
            roots_text = (
                f"Збережений корінь:\n{old_root}\n\n"
                f"Перевірений поточний шлях:\n{current_root}")
        box = QMessageBox(self)
        box.setWindowTitle("Потрібне поточне розташування даних")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "DupScan не зміг однозначно знайти поточну теку цієї історичної "
            "сесії.\n\n"
            f"{roots_text}\n\n"
            "Сканування ще не починалось, файли не змінювались. Виберіть "
            "теку, всередині якої зараз лежить колишній вміст кореня.")
        choose = box.addButton(
            "Обрати поточну теку…", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(
            "Скасувати оновлення", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(choose)
        box.setEscapeButton(cancel)
        return box, choose

    def _confirm_manual_refresh_root(
            self, old_root: str, current_root: str) -> bool:
        box, choose = self._refresh_root_choice_box(old_root, current_root)
        box.exec()
        return box.clickedButton() is choose

    def _offer_session_refresh_root(
            self, previous: core.ScanResult, historical_roots: tuple[str, ...],
            context_token: int, state) -> None:
        old_root, current_root, samples, _is_dir, _is_link, _hits = state[:6]
        specs = state[7]
        if not self._confirm_manual_refresh_root(old_root, current_root):
            self._cancel_session_refresh_preflight(
                "Оновлення сесії скасовано; історичний snapshot лишився.")
            return
        selected = list(dict.fromkeys(pick_dirs(
            self,
            title=f"Де зараз знаходиться {old_root}?",
            accept_label="Обрати поточний корінь",
        )))
        if len(selected) != 1:
            self._cancel_session_refresh_preflight(
                "Оновлення сесії скасовано: потрібна рівно одна коренева тека.")
            return
        new_root = os.path.normpath(os.path.abspath(selected[0]))

        def validate():
            translated = tuple(
                rebase_session_path(path, old_root, new_root)
                for path in samples
            )
            return (
                _real_directory(new_root),
                os.path.islink(new_root),
                sum(
                    _session_probe_matches(
                        path,
                        specs.get(os.path.normpath(os.path.abspath(old_path))),
                    )
                    for old_path, path in zip(
                        samples, translated, strict=True)
                ),
                translated,
            )

        def validated(value) -> None:
            if (
                context_token != self._session_context_token
                or previous is not self.result
                or not self._refresh_preflighting
            ):
                return
            is_dir, is_link, hits, translated = value
            if not is_dir or is_link or (samples and hits == 0):
                example = translated[0] if translated else new_root
                QMessageBox.warning(
                    self, "Корінь не підтверджено",
                    "Обрана тека має бути реальною текою, не symlink. Якщо "
                    "історична сесія містить контрольні шляхи, хоча б один "
                    "мусить існувати у тому самому відносному місці.\n\n"
                    f"Старий корінь:\n{old_root}\n\n"
                    f"Обраний корінь:\n{new_root}\n\n"
                    f"Приклад очікуваного шляху:\n{example}")
                self._cancel_session_refresh_preflight(
                    "Корінь не підтверджено; сканування не запускалось.")
                return
            self._session_refresh_root_overrides[old_root] = new_root
            self.status.setText(
                f"Підтверджено корінь: {old_root} → {new_root}")
            self._preflight_session_refresh(
                previous, historical_roots, context_token)

        def failed(error: str) -> None:
            if context_token != self._session_context_token:
                return
            self._cancel_session_refresh_preflight(
                f"Не вдалося підтвердити корінь: {error}")

        self._ui_bg_run(validate, validated, failed)

    def _start_session_refresh(
            self, previous: core.ScanResult, historical_roots: tuple[str, ...],
            current_roots: list[str], context_token: int) -> None:
        if (
            context_token != self._session_context_token
            or previous is not self.result
            or not self._refresh_preflighting
        ):
            return
        self._refresh_preflight_cancel = None
        self._refresh_preflighting = False
        root_map = dict(self._session_refresh_root_overrides)
        worker = SessionRefreshWorker(
            current_roots, previous, session_roots=historical_roots,
            root_map=root_map, source_session_path=self._loaded_session_path)
        self.refresh_worker = worker
        self._progress = None
        self._eta.reset()
        self.b_pause.setEnabled(True)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self._set_operation_controls_visible(True)
        self.status.setText("Оновлюю сесію: перевіряю структуру коренів…")
        def restore_controls() -> None:
            self._session_refresh_root_overrides.clear()
            self.b_scan.setEnabled(True)
            self.b_pause.setEnabled(False)
            self.b_pause.setText("Пауза")
            self.b_cancel.setEnabled(False)
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            self._set_operation_controls_visible(False)
            self.nav_compare.setEnabled(True)

        def stale() -> bool:
            return (
                self.refresh_worker is not worker
                or context_token != self._session_context_token
                or previous is not self.result
            )

        def progress(phase: str, done: int, total: int) -> None:
            if not stale() and not self._closing:
                self.on_progress(phase, done, total)

        worker.progress.connect(progress)

        def done(fresh: core.ScanResult) -> None:
            if stale() or self._closing:
                return
            self.refresh_worker = None
            restore_controls()
            self.result = fresh
            self._dir_dates = dict(worker.dir_dates)
            self._last_roots = list(current_roots)
            self._session_context_token += 1
            self._loaded_session_roots = ()
            self._loaded_session_path = None
            self._session_root_map.clear()
            self._session_refresh_root_overrides.clear()
            self._session_relinking = False
            for root in current_roots:
                self.folders.add_dir(root)
            stats = worker.stats
            self._pending_result_note = (
                "Дані актуальні — дублікати можна обробляти. "
                f"Додано {stats['added']:,}, видалено "
                f"{stats['removed']:,}, змінено {stats['changed']:,}; "
                f"підготовлено валідних BLAKE3: {worker.reused_hashes:,}.")
            self._update_refresh_session_button(fresh)
            self._refresh_models_deferred = False
            self._refresh_models()
            self._show_task(workflow_ui.TASK_RESULTS)

        def cancelled() -> None:
            if stale() or self._closing:
                return
            self.refresh_worker = None
            restore_controls()
            self.b_refresh_session.setEnabled(True)
            self._restore_deferred_historical_models()
            self.status.setText(
                "Оновлення скасовано. Історичний snapshot лишився без змін.")

        def failed(message: str) -> None:
            if stale() or self._closing:
                return
            self.refresh_worker = None
            restore_controls()
            self.b_refresh_session.setEnabled(True)
            self._restore_deferred_historical_models()
            QMessageBox.warning(
                self, "Не вдалося оновити сесію",
                "Історичний snapshot лишився без змін.\n\n"
                f"Причина:\n{message}")
            self.status.setText(
                "Оновлення не завершено; історичні результати збережено.")

        worker.done.connect(done)
        worker.cancelled.connect(cancelled)
        worker.failed.connect(failed)
        worker.start()

    def _toggle_current_trash_mark(self) -> None:
        model, check_index, path = self._current_check_index()
        if model is None or not check_index.isValid() or path is None:
            QMessageBox.information(
                self, "DupScan",
                "Оберіть конкретний файл або папку. Для подібних папок "
                "оберіть комірку на боці A чи B.")
            return
        wanted = Qt.Unchecked if path in model.checked else Qt.Checked
        model.setData(check_index, wanted, Qt.CheckStateRole)

    def _trash_marked_on_current_tab(self) -> None:
        model, _view = self._active_model_view()
        if isinstance(model, GroupModel):
            self.delete_checked(model)
        elif isinstance(model, SimModel):
            self.delete_checked_sim()

    def _trash_current_duplicate(self) -> None:
        model, check_index, path = self._current_check_index()
        if model is None or not check_index.isValid() or path is None:
            QMessageBox.information(
                self, "DupScan",
                "Оберіть синім конкретний дублікат. На вкладці подібності "
                "клацніть файл саме на боці A або B.")
            return
        if isinstance(model, GroupModel):
            self._delete_duplicate_paths(model, {path})
        elif isinstance(model, SimModel):
            self._sim_trash_flow(lambda p=path: [p])
        else:
            # _active_model_view() навмисно
            # НЕ включає ClusterModel/PerceptualModel — model
            # тут МАЄ бути None/GroupModel/SimModel; цей рядок недосяжний,
            # доки той інваріант тримається. Явна відмова замість
            # мовчазного "not GroupModel -> мабуть Sim", якщо колись
            # порушиться.
            raise TypeError(
                f"_trash_current_duplicate: непідтримуваний тип моделі "
                f"{type(model).__name__} — Кошик дозволено лише для "
                f"GroupModel/SimModel.")

    def _duplicate_menu(self, view: QTreeView, model: GroupModel, pos) -> None:
        index = view.indexAt(pos)
        if not index.isValid():
            return
        view.setCurrentIndex(index)
        self._update_action_state()
        path = model._path(index)
        menu = QMenu(view)
        if path:
            res = self.result
            # D6: historical/partial groups get an honest "Перевірити й…"
            # workaround (fresh proof of exactly this group) instead of a
            # blanket refusal — mirrors _sim_menu's needs_pair_check.
            needs_group_check = bool(res and (not res.live or res.partial))
            if needs_group_check:
                menu.addAction(
                    "Перевірити й прибрати вибраний → Кошик "
                    "(свіжа перевірка групи)",
                    lambda p=path: self._verify_snapshot_group_then_trash(
                        model, {p}))
                if model.checked:
                    menu.addAction(
                        f"Перевірити й прибрати позначені ({len(model.checked)}) "
                        "→ Кошик (свіжа перевірка групи)",
                        lambda: self._verify_snapshot_group_then_trash(
                            model, set(model.checked)))
                menu.addAction(self.action_toggle_trash_mark)
            else:
                menu.addAction(self.action_trash_current)
                menu.addAction(self.action_toggle_trash_mark)
                if model.checked:
                    menu.addAction(self.action_trash_marked)
            menu.addSeparator()
            menu.addAction("Quick Look", lambda p=path: self._open_result_path(p, quick_look))
            menu.addAction("Показати у Finder", lambda p=path: self._open_result_path(p, reveal))
        else:
            menu.addAction("Розгорнути групу", lambda i=index: view.expand(i))
        menu.exec(view.viewport().mapToGlobal(pos))

    def _review_selection(self, selected: set[str], groups: list[list[str]],
                          *, directory_mode: bool = False,
                          action_text: str = "Продовжити перевірку",
                          result: core.ScanResult | None = None) -> set[str] | None:
        """Open the product review surface without touching the disk."""
        res = result or self.result
        if res is None:
            return None
        # Automated smoke/tests use Qt's non-interactive platform; a modal
        # human review has no possible respondent there. Bypass requires
        # pytest too — offscreen alone is settable outside tests.
        if _review_bypass_allowed():
            return set(selected)
        relevant = [list(group) for group in groups if selected.intersection(group)]
        ordered_paths = list(dict.fromkeys(
            path for group in relevant for path in group
        ))
        for path in sorted(selected):
            if path not in ordered_paths:
                ordered_paths.append(path)
                relevant.append([path])
        directory_sizes = {
            path: group.size for group in self.m_dirs.groups for path in group.paths
        }
        entries: list[dict] = []
        for path in ordered_paths:
            meta = res.file_meta.get(path)
            dates = self._dir_dates.get(path)
            if directory_mode:
                reason = (
                    "Структура, назви, symlink-маніфест і повний вміст збігаються. "
                    "Перед Кошиком обидві теки будуть перечитані заново.")
            else:
                reason = product.duplicate_explanation(res, path)
            entries.append({
                "path": path,
                "size": directory_sizes.get(path, int(meta.size) if meta else 0),
                "mtime_ns": (dates[1] if dates else int(meta.mtime_ns) if meta else 0),
                "category": "folder" if directory_mode else product.category_for_path(path),
                "reason": reason,
            })
        dialog = ReviewDialog(
            self, entries, selected, relevant, action_text=action_text)
        if dialog.exec() != QDialog.Accepted:
            return None
        return dialog.selected_paths

    def compare_two_folders(self) -> None:
        self._folder_comparison_ctl.compare_two_folders()

    def _show_folder_comparison(self, dir_a: str, dir_b: str) -> None:
        self._folder_comparison_ctl.show_folder_comparison(dir_a, dir_b)

    def show_removal_history(self) -> None:
        self._removal_history_ctl.show_removal_history()

    # ---- сканування
    def add_dirs(self) -> None:
        paths = pick_dirs(
            self,
            title="Додати папки й диски",
            accept_label="Додати джерела",
        )
        if paths:
            self._add_dirs_async(paths)

    def _add_dirs_async(self, paths: list[str]) -> None:
        candidates = list(dict.fromkeys(os.path.abspath(p) for p in paths if p))
        if not candidates:
            return
        self.status.setText("Перевіряю вибрані теки…")

        def checked():
            return [(path, os.path.isdir(path)) for path in candidates]

        def ready(items):
            valid = [path for path, ok in items if ok]
            for path in valid:
                self.folders.add_dir(path)
            invalid = len(items) - len(valid)
            self.status.setText(
                f"Додано джерел: {len(valid)}"
                + (f" · пропущено недоступних: {invalid}" if invalid else "")
            )

        self._ui_bg_run(checked, ready)

    def start_scan(self) -> None:
        if (self._trashing or self._merging or self._loading
                or self._storage_preflighting
                or self._refresh_preflighting
                or (self.worker and self.worker.isRunning())
                or (self.refresh_worker and self.refresh_worker.isRunning())
                or (self.pair_worker and self.pair_worker.isRunning())
                or (self._rw and self._rw.isRunning())
                or any(w.isRunning() for w in self._bg)):
            QMessageBox.information(
                self, "DupScan", "Зачекайте завершення поточної операції.")
            return
        roots = self.folders.checked_dirs()
        if not roots:
            QMessageBox.information(self, "DupScan",
                                    "Позначте хоча б одну теку у списку.")
            return
        self._compare_after_scan = None
        self._preflight_storage_then_scan(roots)

    def _preflight_storage_then_scan(self, roots: list[str]) -> None:
        """Probe the private data volume without blocking Qt's event loop."""
        self._storage_preflight_token += 1
        token = self._storage_preflight_token
        expected_roots = tuple(roots)
        self._storage_preflighting = True
        self.b_scan.setEnabled(False)
        self.status.setText("Перевіряю вільне місце для кешу й сесії…")

        def finish(status: storage_guard.StorageStatus) -> None:
            if token != self._storage_preflight_token or self._closing:
                return
            self._storage_preflighting = False
            self.b_scan.setEnabled(True)
            if tuple(self.folders.checked_dirs()) != expected_roots:
                self.status.setText(
                    "Список джерел змінився. Натисніть «Почати сканування» "
                    "ще раз.")
                return
            if status.level is storage_guard.StorageLevel.SUFFICIENT:
                self._begin_scan(list(expected_roots))
                return
            if status.level is storage_guard.StorageLevel.CRITICAL:
                free = storage_amount(status.free_bytes or 0)
                required = storage_amount(status.required_bytes or 0)
                QMessageBox.warning(
                    self,
                    "Недостатньо місця для безпечного сканування",
                    f"На томі даних DupScan вільно лише {free}.\n\n"
                    f"Забезпечте щонайменше {required} вільного місця, "
                    "щоб macOS, кеш і "
                    "нова сесія мали робочий резерв. Сканування не розпочато.",
                )
                self.status.setText(
                    "Сканування не розпочато: критично мало вільного місця.")
                return
            if self._confirm_storage_warning(status):
                self._begin_scan(list(expected_roots))
            else:
                self.status.setText(
                    "Сканування скасовано до читання файлів. "
                    "Звільніть місце й повторіть.")

        def failed(message: str) -> None:
            finish(storage_guard.StorageStatus(
                storage_guard.StorageLevel.UNKNOWN,
                preferences.default_data_dir(),
                "",
                None,
                None,
                None,
                message,
            ))

        self._bg_run(storage_guard.probe_storage, finish, failed)

    def _confirm_storage_warning(
        self,
        status: storage_guard.StorageStatus,
    ) -> bool:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        if status.level is storage_guard.StorageLevel.LOW:
            dialog.setWindowTitle("Мало вільного місця")
            dialog.setText(
                "На томі даних DupScan вільно "
                f"{storage_amount(status.free_bytes or 0)}."
            )
            dialog.setInformativeText(
                "Рекомендований резерв — "
                f"{storage_amount(status.required_bytes or 0)}. "
                "Пошук може завершитися, але кеш або історична сесія можуть "
                "не зберегтися. Продовжити свідомо?"
            )
        else:
            dialog.setWindowTitle("Не вдалося перевірити вільне місце")
            dialog.setText(
                "DupScan не може підтвердити запас місця для кешу й сесії."
            )
            detail = status.error or "невідома помилка файлової системи"
            dialog.setInformativeText(
                f"Причина: {detail}\n\n"
                "Продовжуйте лише якщо самостійно перевірили вільне місце."
            )
        proceed = dialog.addButton(
            "Продовжити", QMessageBox.ButtonRole.AcceptRole)
        cancel = dialog.addButton(
            "Скасувати", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        return dialog.clickedButton() is proceed

    def _begin_scan(self, roots: list[str]) -> None:
        self._last_roots = list(roots)
        self._pending_result_note = ""
        self._refresh_models_deferred = False
        self._session_context_token += 1
        self._loaded_session_roots = ()
        self._loaded_session_path = None
        self._session_root_map.clear()
        self._session_refresh_root_overrides.clear()
        self._session_relinking = False
        self._progress = None
        self._eta.reset()
        self.b_scan.setEnabled(False)
        self.b_pause.setEnabled(True)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self._set_operation_controls_visible(True)
        self._show_task(workflow_ui.TASK_SCAN)
        # A profile whose settings are exactly the built-in defaults uses the
        # legacy manifest semantics (symlinks represented, never followed).
        profile = self._profile
        default = preferences.DEFAULT_PROFILE
        effective_profile = None if (
            profile.min_size == default.min_size
            and profile.include_extensions == default.include_extensions
            and profile.excluded_extensions == default.excluded_extensions
            and profile.excluded_paths == default.excluded_paths
            and profile.include_hidden == default.include_hidden
            and profile.include_bundles == default.include_bundles
            and profile.include_symlinks == default.include_symlinks
        ) else profile
        self.worker = ScanWorker(roots, profile=effective_profile)
        worker = self.worker
        context_token = self._session_context_token
        def progress(phase: str, done: int, total: int) -> None:
            if (
                self.worker is worker
                and context_token == self._session_context_token
                and not self._closing
            ):
                self.on_progress(phase, done, total)

        self.worker.progress.connect(progress)
        self.worker.done.connect(
            lambda result, worker=worker, token=context_token:
            self.on_done(result, _worker=worker, _context_token=token))
        self.worker.failed.connect(
            lambda message, worker=worker, token=context_token:
            self.on_scan_failed(
                message, _worker=worker, _context_token=token))
        self.worker.start()
        self.nav_results.setEnabled(False)
        self.nav_compare.setEnabled(False)

    def on_scan_failed(
            self, message: str, *, _worker=None,
            _context_token: int | None = None) -> None:
        if (
            _worker is not None
            and (
                self.worker is not _worker
                or _context_token != self._session_context_token
            )
        ):
            return
        self._compare_after_scan = None
        if self._closing:
            return
        self.b_scan.setEnabled(True)
        self.b_pause.setEnabled(False)
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_operation_controls_visible(False)
        QMessageBox.critical(self, "DupScan", f"Сканування завершилось помилкою:\n{message}")
        self.status.setText("Сканування не завершено; попередні результати збережено.")
        self._show_task(workflow_ui.TASK_SCAN)

    def _controlled_scan_worker(self):
        if self.refresh_worker and self.refresh_worker.isRunning():
            return self.refresh_worker
        if self.pair_worker and self.pair_worker.isRunning():
            return self.pair_worker
        if self.merge_worker and self.merge_worker.isRunning():
            return self.merge_worker
        if self.worker and self.worker.isRunning():
            return self.worker
        return None

    def toggle_pause(self) -> None:
        worker = self._controlled_scan_worker()
        if worker is None:
            return
        if not worker.pause.is_set():
            worker.pause.set()
            self.b_pause.setText("Продовжити")
            if self.bar.maximum() == 0:
                # busy-бар анімується сам по собі, незалежно від потоків скану;
                # переведення у визначений режим — штатний спосіб Qt зупинити його
                self.bar.setRange(0, 1)
                self.bar.setValue(0)
            self.status.setText(self._paused_status())
            if worker in (self.pair_worker, self.merge_worker):
                changing = bool(getattr(worker, "mutates_files", False))
                self._set_results_operation(
                    (
                        "Операцію призупинено між безпечними файловими "
                        "кроками; вже завершені зміни лишаються."
                        if changing else
                        "Перевірку призупинено. Файли ще не змінюються."
                    )
                    + " Натисніть «Продовжити» або скасуйте операцію.",
                    visible=True,
                )
        else:
            worker.pause.clear()
            self.b_pause.setText("Пауза")
            # миттєвий рендер зі збереженого стану: наступний тик під час
            # хешування величезного файлу може прийти за хвилини
            if self._progress:
                self._render_progress(*self._progress)
            else:
                self.bar.setRange(0, 0)
                self.status.setText("Сканування продовжено…")
            if worker in (self.pair_worker, self.merge_worker):
                self._set_results_operation(
                    (
                        "Файлову операцію продовжено."
                        if getattr(worker, "mutates_files", False)
                        else "Перевірку продовжено. Файли ще не змінюються."
                    ),
                    visible=True,
                )

    def cancel_scan(self) -> None:
        if (
            self._loading
            and self.session_load_worker is not None
            and self.session_load_worker.isRunning()
        ):
            self.session_load_worker.cancel.set()
            self.b_cancel.setEnabled(False)
            self.status.setText(
                "Скасовую завантаження сесії — поточні результати не зміняться…")
            return
        if self._refresh_preflighting:
            if self._refresh_preflight_cancel is not None:
                self._refresh_preflight_cancel.set()
            self._cancel_session_refresh_preflight(
                "Оновлення скасовано до початку сканування. "
                "Історичний знімок лишився без змін.")
            return
        worker = self._controlled_scan_worker()
        if worker is None:
            return
        worker.cancel.set()
        worker.pause.clear()  # розбудити заморожені на паузі потоки
        self.b_pause.setText("Пауза")
        if worker is self.pair_worker:
            self.status.setText("Скасовую перевірку вибраної пари…")
            self._set_results_operation(
                "Скасовую перевірку вибраної пари. Жоден файл не змінено.",
                visible=True,
            )
        elif worker is self.refresh_worker:
            self.status.setText(
                "Скасовую оновлення — історичний snapshot лишиться без змін…")
        elif worker is self.merge_worker:
            if getattr(worker, "mutates_files", False):
                self.status.setText(
                    "Зупиняю після поточного безпечного файлового кроку…")
                self._set_results_operation(
                    "Зупиняю операцію. Незавершений temp буде прибрано; "
                    "вже завершені файли не відкочуються.",
                    visible=True,
                )
            else:
                self.status.setText(
                    "Скасовую підготовку злиття — файли ще не змінювались…")
                self._set_results_operation(
                    "Скасовую підготовку злиття. Файли ще не змінювались.",
                    visible=True,
                )
        else:
            self.status.setText("Скасовую — зберігаю часткові результати…")

    def _paused_status(self) -> str:
        if self._progress is None:
            return "⏸ Призупинено. «Продовжити» відновить сканування."
        phase, done, total = self._progress
        if total > 0:
            return (f"⏸ Призупинено — {phase}: {done} з {total} "
                    f"(лишилось {total - done}). «Продовжити» відновить.")
        return f"⏸ Призупинено — {phase}: {done}. «Продовжити» відновить."

    def on_progress(self, phase: str, done: int, total: int) -> None:
        self._progress = (phase, done, total)
        worker = self._controlled_scan_worker()
        if worker and worker.pause.is_set():
            # запізнілий тик під паузою: освіжити числа в паузному тексті,
            # бар лишити замороженим
            self.status.setText(self._paused_status())
            return
        self._render_progress(phase, done, total)
        if worker in (self.pair_worker, self.merge_worker):
            numbers = f"{done:,} / {total:,}" if total > 0 else f"{done:,}"
            suffix = (
                "Завершені файлові кроки лишаються; можна призупинити "
                "або безпечно зупинити."
                if getattr(worker, "mutates_files", False)
                else "Файли ще не змінюються; можна призупинити або скасувати."
            )
            self._set_results_operation(
                f"{phase}: {numbers}. {suffix}",
                visible=True,
            )

    def _render_progress(self, phase: str, done: int, total: int) -> None:
        """Єдина точка, що пише прогрес у бар і статус."""
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
        else:
            self.bar.setRange(0, 0)
        self.status.setText(self._eta.render(phase, done, total))

    def on_done(
            self, result: core.ScanResult, *, _worker=None,
            _context_token: int | None = None) -> None:
        if (
            _worker is not None
            and (
                self.worker is not _worker
                or _context_token != self._session_context_token
            )
        ):
            return
        if self._closing:
            return
        self.result = result
        result_worker = _worker if _worker is not None else self.worker
        if (
            result.live
            and getattr(result_worker, "saved_session_path", None) == ""
        ):
            self._pending_result_note = (
                "УВАГА: результати доступні зараз, але історію цієї "
                "перевірки не збережено. Перевірте вільне місце й доступ "
                "до Application Support."
            )
        self._update_refresh_session_button(result)
        if result.live:
            self._session_context_token += 1
            self._loaded_session_roots = ()
            self._loaded_session_path = None
            self._session_root_map.clear()
            self._session_refresh_root_overrides.clear()
            self._session_relinking = False
        self.b_scan.setEnabled(True)
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        self._set_operation_controls_visible(False)
        # сесію і дати тек уже зробив скан-потік (ScanWorker.run) — тут лише UI
        self._dir_dates = dict(self.worker.dir_dates) if self.worker else {}
        self._refresh_models()
        comparison = self._compare_after_scan
        self._compare_after_scan = None
        if comparison is not None:
            self._show_task(workflow_ui.TASK_COMPARE)
            QTimer.singleShot(
                0, lambda a=comparison[0], b=comparison[1]:
                self._show_folder_comparison(a, b))
        else:
            self._show_task(
                workflow_ui.task_after_scan(cancelled=result.partial))

    def _manage_reference_roots(self) -> None:
        """Керування теками-еталонами: список + додати/зняти.

        Зняття — з підтвердженням: користувач знімає ЗАХИСТ, а не мітку
        краси. Кожна зміна — save (realpath на записі) + перезавантаження
        GUI-кеша, щоб перший шар (чекбокси) побачив зміну одразу."""
        try:
            roots = list(preferences.load_reference_roots())
        except preferences.PreferencesError as error:
            QMessageBox.warning(
                self, "DupScan",
                f"Список тек-еталонів не читається: {error}\n"
                "До виправлення деструктивні дії заблоковано (fail-closed).")
            return
        listing = "\n".join(roots) if roots else "(порожньо)"
        box = QMessageBox(self)
        box.setWindowTitle("Теки-еталони")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "Тека-еталон — недоторканна: з неї нічого не можна видалити чи "
            f"злити, докладати в неї — можна.\n\nЗараз еталони:\n{listing}")
        add_button = box.addButton(
            "Додати теку…", QMessageBox.ButtonRole.ActionRole)
        remove_button = (
            box.addButton("Зняти захист…", QMessageBox.ButtonRole.ActionRole)
            if roots else None)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        clicked = box.clickedButton()
        if clicked is add_button:
            picked = pick_dirs(
                self, title="Позначити теки-еталони",
                accept_label="Зробити еталоном")
            if picked:
                preferences.save_reference_roots(roots + list(picked))
                self._load_reference_cache()
                self.status.setText(
                    f"Тек-еталонів: {len(self._reference_roots_cache or ())}.")
            return
        if remove_button is not None and clicked is remove_button:
            victim, ok = QInputDialog.getItem(
                self, "Зняти захист еталона",
                "З якої теки зняти захист? Після цього її вміст знову "
                "доступний деструктивним діям.", roots, 0, False)
            if ok and victim:
                confirm = QMessageBox.question(
                    self, "Зняти захист?",
                    f"Зняти статус еталона з:\n{victim}\n\n"
                    "Тека стане звичайною — її вміст зможе потрапляти в "
                    "Кошик за стандартними правилами доказу.")
                if confirm == QMessageBox.StandardButton.Yes:
                    preferences.save_reference_roots(
                        [r for r in roots if r != victim])
                    self._load_reference_cache()
                    self.status.setText("Захист еталона знято.")

    def _load_reference_cache(self) -> None:
        """Кеш тек-еталонів для GUI-шару. None = список не читається =
        fail-closed (блокувати все). Перечитується на старті і після
        кожної зміни списку користувачем."""
        try:
            self._reference_roots_cache: tuple[str, ...] | None = (
                preferences.load_reference_roots())
        except preferences.PreferencesError:
            self._reference_roots_cache = None

    def _reference_blocked(self, path: str) -> bool:
        """Чи блокує тека-еталон дію над path.

        GUI-потік: ЛИШЕ кеш + лексична перевірка, жодного диска (інваріант;
        зловлено тестом, що сповільнює lstat). Обхід через symlink добиває
        повний realpath-рубіж у fsops.to_trash — він завжди у фоні."""
        roots = getattr(self, "_reference_roots_cache", ())
        if roots is None:
            return True
        return preferences.is_protected_lexical(path, roots)

    def _export_merge_plan(self, plan, src_dir: str, dst_dir: str) -> None:
        """Зберегти ПОВНИЙ план злиття у CSV — dry-run перед рішенням.

        Best-effort за визначенням: збій запису звіту ніколи не ламає
        саме злиття. Запис — у фоні (_bg_run):
        план на десятки тисяч рядків не має морозити вікно.
        """
        destination, _selected = QFileDialog.getSaveFileName(
            self, "Зберегти повний план злиття",
            os.path.expanduser("~/Desktop/dupscan-план-злиття.csv"),
            "CSV (*.csv)")
        if not destination:
            return
        rows = list(plan)

        def write() -> str:
            reports.write_merge_plan_csv(destination, rows, src_dir, dst_dir)
            return destination

        self._bg_run(
            write,
            lambda saved: self.status.setText(
                f"План злиття збережено ({len(rows)} рядків): {saved}"),
            lambda error: self.status.setText(
                f"Не вдалося зберегти план: {error}"),
        )

    def _bg_run(self, fn, on_ok, on_err=None) -> None:
        """fn() — у фоновому потоці; on_ok(результат) / on_err(текст) — у GUI."""
        w = Bg(fn)
        self._bg.add(w)

        def _fin(payload, w=w):
            self._bg.discard(w)
            # Closing is a hard UI lifecycle barrier. The worker itself is
            # allowed to finish safely, but its queued callback must not open
            # a dialog or start a follow-up operation on a closing window.
            if self._closing:
                return
            kind, val = payload
            if kind == "ok":
                on_ok(val)
            elif on_err is not None:
                on_err(val)
            else:
                self.status.setText(f"Помилка фонової операції: {val}")

        w.done.connect(_fin)
        w.start()

    def _ui_bg_run(self, fn, on_ok, on_err=None) -> None:
        """Фонова UI-допоміжна робота, що не робить результат небезпечним.

        Пошук не має блокувати новий скан або перевірку файлів. Worker все
        одно утримується до завершення й враховується під час закриття.
        """
        w = Bg(fn)
        self._ui_bg.add(w)

        def _fin(payload, w=w):
            self._ui_bg.discard(w)
            if self._closing:
                return
            kind, val = payload
            if kind == "ok":
                on_ok(val)
            elif on_err is not None:
                on_err(val)
            else:
                self.status.setText(f"Помилка пошуку: {val}")

        w.done.connect(_fin)
        w.start()

    def _refresh_models(self) -> None:
        r = self.result
        if r is None:
            return
        self._model_prepare_token += 1
        token = self._model_prepare_token
        if self._model_prepare_cancel is not None:
            self._model_prepare_cancel.set()
        total_records = (
            sum(len(group.paths) for group in r.file_groups)
            + sum(len(group.paths) for group in r.dir_groups)
            + len(r.sim_pairs)
            + sum(len(pair.shared) for pair in r.sim_pairs)
        )
        if total_records <= 20_000:
            self._model_preparing = False
            self.m_files.set_groups(r.file_groups)
            self.m_dirs.set_groups(r.dir_groups)
            self.m_sim.set_pairs(r.sim_pairs)
            self._finish_model_refresh(r)
            return

        cancel = threading.Event()
        self._model_prepare_cancel = cancel
        self._model_preparing = True
        self.tabs.setEnabled(False)
        self.b_trash_marked.setEnabled(False)
        self.status.setText(
            f"Готую індекс великих результатів у фоні: "
            f"{total_records:,} записів…")
        file_meta = r.file_meta
        dir_dates = dict(self._dir_dates)

        def dates(path: str):
            meta = file_meta.get(path)
            if meta is not None:
                return meta.btime_ns, meta.mtime_ns
            return dir_dates.get(path)

        def prepare():
            files = GroupModel.prepare_groups(
                r.file_groups, dates, cancel)
            if files is None:
                return None
            directories = GroupModel.prepare_groups(
                r.dir_groups, dates, cancel)
            if directories is None:
                return None
            similarities = SimModel.prepare_pairs(r.sim_pairs, cancel)
            if similarities is None or cancel.is_set():
                return None
            return files, directories, similarities

        def ready(payload) -> None:
            if (payload is None or token != self._model_prepare_token
                    or r is not self.result):
                return
            files, directories, similarities = payload
            self.m_files.apply_prepared_groups(r.file_groups, files)
            self.m_dirs.apply_prepared_groups(r.dir_groups, directories)
            self.m_sim.apply_prepared_pairs(r.sim_pairs, similarities)
            self._model_preparing = False
            self._model_prepare_cancel = None
            self.tabs.setEnabled(True)
            self._finish_model_refresh(r)

        def failed(error: str) -> None:
            if token != self._model_prepare_token:
                return
            self._model_preparing = False
            self._model_prepare_cancel = None
            self.tabs.setEnabled(True)
            self.status.setText(
                f"Не вдалося підготувати представлення результатів: {error}")

        self._ui_bg_run(prepare, ready, failed)

    def _finish_model_refresh(self, r: core.ScanResult) -> None:
        wasted = sum(g.wasted for g in r.file_groups)
        problem_count = _problem_count(r)
        err = f" · помилок: {problem_count}" if problem_count else ""
        partial = " · ЧАСТКОВИЙ (скасовано)" if r.partial else ""
        note = self._pending_result_note
        self._pending_result_note = ""
        self.status.setText(
            f"Результати готові{err}{partial}"
            + (f" · {note}" if note else ""))
        self._update_refresh_session_button(r)
        read_only = (
            # «ЛИШЕ ПЕРЕГЛЯД» стосується ГРУП: там немає обмеженої області,
            # яку можна чесно перевірити наново. Пара A/B — має, тому для
            # неї злиття доступне зі свіжою перевіркою рівно цих двох тек.
            "ЛИШЕ ПЕРЕГЛЯД для груп — пару A/B можна злити після свіжої "
            "перевірки обраних тек.  "
            if not r.live or r.partial else ""
        )
        self.summary.setText(
            f"{read_only}Файлів: {r.files_seen:,}   •   Даних: {human(r.bytes_seen)}   •   "
            f"Груп файлів: {len(r.file_groups)}   •   Груп папок: {len(r.dir_groups)}   •   "
            f"Подібних пар: {len(r.sim_pairs)}   •   До звільнення: {human(wasted)}")
        self._update_result_quality(r)
        self.tabs.setTabText(0, f"Файли-дублікати ({len(r.file_groups)})")
        self.tabs.setTabText(1, f"Папки-дублікати ({len(r.dir_groups)})")
        self.tabs.setTabText(2, f"Подібність папок ({len(r.sim_pairs)})")
        # Чисто, без I/O (build_duplicate_
        # clusters читає лише вже готовий r) — синхронно тут само, як
        # wasted/problem_count вище; лінійно за виміром (perf-тест
        # test_large_result_stays_roughly_linear), не окремий фоновий крок.
        cluster_list = clusters.build_duplicate_clusters(r)
        self.m_clusters.set_clusters(cluster_list)
        self.tabs.setTabText(3, f"Кластери тек ({len(cluster_list)})")
        # Перцептивні результати НЕ
        # персистяться і не відносяться до ІНШОГО об'єкта результату (нове
        # сканування чи щойно завантажена сесія) — застарілу підказку
        # прибираємо, а не лишаємо мотлохом на вкладці. Historical сесія:
        # чесне пояснення замість тихо порожньої вкладки чи кнопки
        # сканування, що виглядає доступною, але не рахує нічого нового.
        if r is not self._perceptual_scanned_for:
            self.m_perceptual.set_groups([])
            self.tabs.setTabText(4, "Схожі фото (підказка)")
            if not r.live:
                self.l_perceptual_status.setText(
                    "Завантажена сесія доступна лише для перегляду: "
                    "перцептивні результати не зберігаються в сесії. "
                    "Виконайте нове сканування, щоб перевірити фото.")
                self.b_perceptual_scan.setEnabled(False)
            else:
                self.l_perceptual_status.setText(
                    "Натисніть «Сканувати фото…», щоб знайти візуально "
                    "схожі зображення серед сканованих джерел.")
                self.b_perceptual_scan.setEnabled(True)
        # Вкладка «Проблеми» — та сама errors/
        # errors_total поверхня, що вже показують ProblemsDialog/
        # _update_result_quality вище; лічильник у назві вкладки лише коли
        # є що рахувати (0 → без числа, як «Кластери тек»/перцептив).
        self.m_problems.set_errors(list(r.errors), problem_count)
        self.tabs.setTabText(
            5, f"Проблеми ({problem_count:,})" if problem_count else "Проблеми")
        self.l_problems_status.setText(
            "Проблем не виявлено." if not problem_count else
            f"Категорій: {len(self.m_problems.categories)} · усього: "
            f"{problem_count:,}.")
        # set_errors() щойно скинула модель — currentIndex()
        # більше не вказує на живий рядок, тож кнопка мусить знову стати
        # неактивною, доки користувач не обере рядок заново.
        self._update_problems_namefix_button()
        self._update_selection_summary()
        for v in (
                self.v_files, self.v_dirs, self.v_sim, self.v_clusters,
                self.v_perceptual, self.v_problems):
            self._fill_columns(v)

    # ---- автоперерахунок: повернення у застосунок після видалень у Finder
    def _on_app_state(self, state) -> None:
        if state != Qt.ApplicationActive or not self.result:
            return
        # A historical result is immutable. Old absolute paths may only have
        # moved with their volume root; pruning them would remove the pair
        # before the user can explicitly relink it.
        if not self.result.live:
            return
        if self._operation_busy():
            return
        if self._sweeping or time.monotonic() - self._last_sweep < 3.0:
            return  # тротлінг: не частіше ніж раз на 3с і без накладань
        r = self.result
        known = {p for g in r.file_groups for p in g.paths}
        known |= {p for g in r.dir_groups for p in g.paths}
        known |= {d for pr in r.sim_pairs for d in (pr.dir_a, pr.dir_b)}
        if not known:
            return
        self._sweeping = True
        self._last_sweep = time.monotonic()

        def check(paths=known):
            # stat по (можливо сплячому) USB — саме тому у фоні, не в GUI
            return {p for p in paths if not os.path.exists(p)}

        def ok(missing):
            self._sweeping = False
            if missing:
                self._recompute_async(
                    missing, f"Виявлено видалення поза застосунком ({len(missing)}).")

        self._bg_run(check, ok, lambda e: setattr(self, "_sweeping", False))

    def _recompute_async(self, removed: set[str], note: str) -> None:
        if self._rw and self._rw.isRunning():
            return  # наступна активація вікна підбере залишки
        self.status.setText(note + " Перераховую результати…")
        self._rw = RecomputeWorker(self.result, removed)
        self._rw.done.connect(lambda: (self._refresh_models(),
                                       self.status.setText(
                                           note + " " + self.status.text())))
        self._rw.failed.connect(
            lambda e: self.status.setText(f"Помилка перерахунку: {e}"))
        self._rw.start()

    # ---- історія сканів: список (сайдкари, фоново), завантаження,
    # експорт у файл, імпорт, видалення
    def _session_load_choice_box(
            self, meta: dict, parent: QWidget | None = None,
            ) -> tuple[QMessageBox, QPushButton, QPushButton]:
        roots = "\n".join(meta.get("roots", [])) or "Корені не записані"
        box = QMessageBox(parent if parent is not None else self)
        box.setWindowTitle("Відкрити історичну сесію")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(
            "Як відкрити збережений знімок?\n\n"
            f"Збережені корені:\n{roots}\n\n"
            "«Інтелектуальний рескан і працювати» знайде поточне "
            "розташування даних, "
            "актуалізує сесію у фоні й лише після успіху дозволить обробку "
            "дублікатів. Старий файл сесії не зміниться.\n\n"
            "«Лише переглянути знімок» покаже історичні дані без права "
            "видалення.")
        refresh = box.addButton(
            "Інтелектуальний рескан і працювати",
            QMessageBox.ButtonRole.AcceptRole)
        view = box.addButton(
            "Лише переглянути знімок", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(
            "Скасувати", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(refresh)
        box.setEscapeButton(cancel)
        return box, refresh, view

    def _choose_session_load_mode(
            self, meta: dict, parent: QWidget | None = None,
            ) -> str | None:
        box, refresh, view = self._session_load_choice_box(meta, parent)
        box.exec()
        clicked = box.clickedButton()
        if clicked is refresh:
            return "refresh"
        if clicked is view:
            return "view"
        return None

    def show_history(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Історія сканувань")
        dlg.resize(660, 440)
        v = QVBoxLayout(dlg)
        info = QLabel("Завантажую список…")
        lw = QListWidget()
        v.addWidget(info)
        v.addWidget(lw, 1)

        def fill(metas: list) -> None:
            lw.clear()
            for meta in metas:
                c = meta.get("counts", {})
                label = (f"{when(meta['created_ns'])} · {meta.get('files_seen', 0)} файлів"
                         f" · груп {c.get('files', 0)}/{c.get('dirs', 0)}/{c.get('pairs', 0)}")
                if meta.get("partial"):
                    label += " · частковий"
                it = QListWidgetItem(label)
                it.setData(Qt.UserRole, meta)
                it.setToolTip("\n".join(meta.get("roots", [])))
                lw.addItem(it)
            info.setText(f"Сесій: {lw.count()}" if lw.count() else
                         "Історія порожня. Сесії зберігаються самі після скану.")

        def reload_list() -> None:
            info.setText("Завантажую список…")
            self._bg_run(session.list_sessions, fill,
                         lambda e: info.setText(f"Помилка списку: {e}"))

        reload_list()

        row = QHBoxLayout()
        b_load = QPushButton("Відкрити…")
        b_exp = QPushButton("Зберегти у файл…")
        b_imp = QPushButton("Імпортувати…")
        b_del = QPushButton("Видалити")
        b_compact = QPushButton("Прибрати старі…")
        b_compact.setEnabled(False)
        b_compact.setToolTip(
            "Видалити незжаті сесії, старші за 30 днів, і найстаріші "
            "сесії понад бюджет сховища (2 ГБ). Найновіша та відкрита "
            "сесії лишаються.")
        b_close = QPushButton("Закрити")
        for b in (b_load, b_exp, b_imp, b_del, b_compact):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)

        # Прибирання сховища — ЛИШЕ явною дією: діалог сам нічого не
        # видаляє, dry-run тільки рахує підпис для кнопки.
        compact_plan = {"removed": 0, "freed": 0}

        def _compact_preserve() -> tuple:
            return tuple(
                p for p in (self._loaded_session_path,) if p)

        def refresh_compact_label() -> None:
            preserve = _compact_preserve()

            def probe() -> dict:
                return session.compact_store(
                    legacy_age_days=30,
                    max_bytes=session._MAX_SESSION_STORE_BYTES,
                    preserve_paths=preserve, dry_run=True)

            def apply(report: dict) -> None:
                compact_plan["removed"] = report.get("removed", 0)
                compact_plan["freed"] = report.get("freed_bytes", 0)
                if compact_plan["removed"] > 0:
                    b_compact.setText(
                        f"Прибрати старі ({human(compact_plan['freed'])})")
                    b_compact.setEnabled(True)
                else:
                    b_compact.setText("Прибрати старі (нема чого)")
                    b_compact.setEnabled(False)

            self._bg_run(probe, apply,
                         lambda _e: b_compact.setEnabled(False))

        refresh_compact_label()

        def do_compact() -> None:
            if compact_plan["removed"] <= 0:
                return
            answer = QMessageBox.question(
                dlg, "Прибрати старі сесії",
                f"Видалити {compact_plan['removed']} стар. сесій і "
                f"звільнити {human(compact_plan['freed'])}?\n\n"
                "Найновіша та відкрита сесії лишаться. "
                "Дію не можна скасувати.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            preserve = _compact_preserve()
            info.setText("Прибираю старі сесії…")
            self._ui_bg_run(
                lambda: session.compact_store(
                    legacy_age_days=30,
                    max_bytes=session._MAX_SESSION_STORE_BYTES,
                    preserve_paths=preserve),
                lambda report: (
                    info.setText(
                        f"Прибрано {report['removed']} сесій, звільнено "
                        f"{human(report['freed_bytes'])}."),
                    reload_list(),
                    refresh_compact_label(),
                ),
                lambda e: info.setText(f"Помилка прибирання: {e}"),
            )

        def picked() -> dict | None:
            it = lw.currentItem()
            return it.data(Qt.UserRole) if it else None

        def do_load() -> None:
            meta = picked()
            if meta:
                mode = self._choose_session_load_mode(meta, dlg)
                if mode is None:
                    return
                dlg.accept()
                self._load_session(
                    meta["path"], meta.get("roots", []),
                    refresh_after_load=mode == "refresh")

        def do_export() -> None:
            meta = picked()
            if not meta:
                return
            stamp = when(meta["created_ns"]).replace(":", "-").replace(" ", "_")
            dst, _ = QFileDialog.getSaveFileName(
                dlg, "Зберегти сесію у файл",
                os.path.join(os.path.expanduser("~/Desktop"),
                             f"dupscan-сесія-{stamp}.dupscan"),
                "Сесії DupScan (*.dupscan *.json *.json.gz)")
            if not dst:
                return
            info.setText("Зберігаю у файл…")
            self._bg_run(lambda: session.export_session(meta["path"], dst),
                         lambda _v: info.setText(f"Збережено: {dst}"),
                         lambda e: info.setText(f"Помилка збереження: {e}"))

        def do_import() -> None:
            src, _ = QFileDialog.getOpenFileName(
                dlg, "Імпортувати сесію", os.path.expanduser("~"),
                "Сесії DupScan (*.dupscan *.json *.json.gz)")
            if not src:
                return
            info.setText("Імпортую…")
            self._bg_run(lambda: session.import_session(src),
                         lambda _p: reload_list(),
                         lambda e: info.setText(f"Помилка імпорту: {e}"))

        def do_delete() -> None:
            meta = picked()
            if meta:
                info.setText("Видаляю сесію…")
                self._ui_bg_run(
                    lambda: session.delete_session(meta["path"]),
                    lambda _v: reload_list(),
                    lambda e: info.setText(f"Помилка видалення: {e}"),
                )

        b_load.clicked.connect(do_load)
        b_exp.clicked.connect(do_export)
        b_imp.clicked.connect(do_import)
        b_del.clicked.connect(do_delete)
        b_compact.clicked.connect(do_compact)
        b_close.clicked.connect(dlg.close)
        dlg.exec()

    def _load_session_active(self, op: _SessionLoadOp) -> bool:
        return (
            self.session_load_worker is op.worker
            and op.context_token == self._session_context_token
            and not self._closing
        )

    def _load_session_restore_controls(self) -> None:
        self._loading = False
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_operation_controls_visible(False)

    def _load_session_ok(self, op: _SessionLoadOp, val) -> None:
        if not self._load_session_active(op):
            return
        self._load_session_restore_controls()
        res, dates = val
        self.result = res
        self._last_roots = list(op.roots)
        self._loaded_session_roots = tuple(dict.fromkeys(
            os.path.normpath(os.path.abspath(root)) for root in op.roots
        ))
        self._loaded_session_path = os.path.normpath(os.path.abspath(op.path))
        self._session_root_map.clear()
        self._session_refresh_root_overrides.clear()
        self._dir_dates = dates
        # Parsing is enough to start authoritative refresh. Do not hide
        # this action behind large in-memory model indexing.
        self._update_refresh_session_button(res)
        self._show_task(workflow_ui.TASK_RESULTS)
        if op.refresh_after_load and self._loaded_session_roots:
            self._refresh_models_deferred = True
            self._show_session_refresh_placeholder()
            self.status.setText(
                "Історичний знімок прочитано. Визначаю поточне "
                "розташування та актуалізую дані…")
            self._begin_loaded_session_refresh(
                res, self._loaded_session_roots)
            return
        for root in op.roots:
            self.folders.add_dir(root)
        self._refresh_models()
        self.status.setText("Завантажено історичний знімок. "
                            + self.status.text())
        self._last_sweep = 0.0  # зниклі шляхи підбере фоновий sweep одразу
        self._on_app_state(Qt.ApplicationActive)
        if op.refresh_after_load:
            QMessageBox.warning(
                self, "Немає коренів сесії",
                "Збережений знімок не містить коренів для актуалізації. "
                "Він відкритий лише для перегляду.")

    def _load_session_err(self, op: _SessionLoadOp, e: str) -> None:
        if not self._load_session_active(op):
            return
        self._load_session_restore_controls()
        QMessageBox.warning(self, "DupScan", f"Не вдалось завантажити сесію: {e}")

    def _load_session_cancelled(self, op: _SessionLoadOp) -> None:
        if not self._load_session_active(op):
            return
        self._load_session_restore_controls()
        self.status.setText(
            "Завантаження сесії скасовано. Поточні результати не змінено.")

    def _load_session(
            self, path: str, roots: list[str], *,
            refresh_after_load: bool = False) -> None:
        if self._session_refresh_busy():
            QMessageBox.information(self, "DupScan", "Зачекайте: триває інша операція.")
            return
        if self._loading:
            return
        # A QThread may already be finished while its queued done/failed
        # callback still waits in the GUI event loop.  Loading a new session
        # invalidates those callbacks and owns the operation controls.
        for name in (
                "worker", "session_load_worker", "refresh_worker",
                "pair_worker", "merge_worker"):
            finished = getattr(self, name)
            if finished is not None and not finished.isRunning():
                setattr(self, name, None)
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self.bar.setValue(0)
        self._set_operation_controls_visible(True)
        self._cancel_model_preparation_for_replacement()
        self._loading = True
        self._pending_result_note = ""
        self._refresh_models_deferred = False
        self._session_context_token += 1
        if self._refresh_preflight_cancel is not None:
            self._refresh_preflight_cancel.set()
        self._refresh_preflight_cancel = None
        self._loaded_session_roots = ()
        self._loaded_session_path = None
        self._session_root_map.clear()
        self._session_refresh_root_overrides.clear()
        self._session_relinking = False
        self.status.setText("Завантажую сесію…")

        op = _SessionLoadOp(
            path=path, roots=roots, refresh_after_load=refresh_after_load,
            context_token=self._session_context_token,
        )
        worker = SessionLoadWorker(
            path, load_directory_dates=not refresh_after_load)
        op.worker = worker
        self.session_load_worker = worker

        def progress(phase: str, done: int, total: int) -> None:
            if self._load_session_active(op):
                self.on_progress(phase, done, total)

        worker.progress.connect(progress)
        worker.done.connect(lambda val: self._load_session_ok(op, val))
        worker.cancelled.connect(lambda: self._load_session_cancelled(op))
        worker.failed.connect(lambda e: self._load_session_err(op, e))
        worker.start()

    # ---- видалення: лише Кошик; група ніколи не лишається порожньою
    def _operation_busy(self) -> bool:
        return bool(
            self._loading or self._trashing or self._merging
            or self._refresh_preflighting
            or self._batch_selecting or self._model_preparing
            or (self._rw and self._rw.isRunning())
            or (self.worker and self.worker.isRunning())
            or (self.refresh_worker and self.refresh_worker.isRunning())
            or (self.pair_worker and self.pair_worker.isRunning())
            or (self.merge_worker and self.merge_worker.isRunning())
            or any(w.isRunning() for w in self._bg)
        )

    def _destructive_result_ready(self) -> bool:
        res = self.result
        if res is None:
            return False
        if self._operation_busy():
            QMessageBox.information(
                self, "DupScan", "Зачекайте завершення поточної операції.")
            return False
        if not res.live:
            QMessageBox.information(
                self, "DupScan",
                "Завантажена сесія доступна лише для перегляду щодо прямого "
                "видалення. Для пари "
                "подібних тек скористайтеся діями «Перевірити й…»: DupScan "
                "пересканує лише обрані A/B. Для груп дублікатів — те саме "
                "через контекстне меню («Перевірити й прибрати… (свіжа "
                "перевірка групи)»); DupScan повністю перечитає рівно "
                "вибрану групу.")
            return False
        if res.partial:
            QMessageBox.information(
                self, "DupScan",
                "Частковий результат не можна використовувати для видалення. "
                "Завершіть нове сканування.")
            return False
        return True

    def delete_checked(self, model: GroupModel) -> None:
        # Структурна відмова — перцептивна
        # підказка (PerceptualModel) і кластери тек (ClusterModel) не
        # мають .checked, тож без цієї перевірки тут був би AttributeError
        # (випадковий, не навмисний захист). TypeError, не assert — asserts
        # може вирізати PYTHONOPTIMIZE у зібраному .app, а це safety-гейт.
        if not isinstance(model, GroupModel):
            raise TypeError(
                f"delete_checked приймає лише GroupModel (файли/теки-"
                f"дублікати); отримано {type(model).__name__} — "
                f"перцептивна підказка й кластери тек не видаляються "
                f"звідси.")
        self._delete_duplicate_paths(model, set(model.checked))

    def _delete_duplicate_paths(self, model: GroupModel, requested: set[str]) -> None:
        """Review and trash an explicit set through the existing safe pipeline.

        This lets the menu act on the blue current row while checkboxes remain
        a separate batch-selection mechanism. A direct action replaces marks
        only inside the affected groups; marks in unrelated groups stay intact.
        """
        # Та сама явна відмова тут — не лише в delete_checked() —
        # бо _trash_current_duplicate() кличе цю функцію напряму.
        if not isinstance(model, GroupModel):
            raise TypeError(
                f"_delete_duplicate_paths приймає лише GroupModel; "
                f"отримано {type(model).__name__}.")
        if not self._destructive_result_ready():
            return
        if not requested or self._trashing:
            QMessageBox.information(self, "DupScan", "Нічого не позначено.")
            return
        # Тека-еталон: жертва під еталоном зупиняє дію ДО
        # review — щоб людина не витрачала перегляд на завідомо заборонене.
        protected = sorted(
            path for path in requested if self._reference_blocked(path))
        if protected:
            sample = "\n".join(protected[:10])
            if len(protected) > 10:
                sample += f"\n… і ще {len(protected) - 10}"
            QMessageBox.warning(
                self, "DupScan",
                "Дію зупинено: позначені шляхи лежать у теці-еталоні "
                f"(недоторканна за визначенням):\n{sample}")
            return
        res = self.result
        # _destructive_result_ready() вище вже повернув би
        # False (і ми вийшли б рядком раніше), якби self.result був None —
        # інваріант з тіла того методу, mypy його через bool не бачить.
        assert res is not None
        directory_mode = model is self.m_dirs
        relevant_groups = [
            list(group.paths) for group in model.groups
            if requested.intersection(group.paths)
        ]
        reviewed = self._review_selection(
            requested, relevant_groups,
            directory_mode=directory_mode)
        if reviewed is None:
            return
        requested = reviewed
        affected_paths = {path for group in relevant_groups for path in group}
        model.checked.difference_update(affected_paths)
        model.checked.update(reviewed)
        model._emit_checks_changed()
        model.checksChanged.emit()
        if not requested:
            self.status.setText("Усі позначки знято під час перевірки.")
            return
        self._trashing = True
        self.status.setText("Повністю перевіряю обидві копії перед Кошиком…")

        def verify():
            victims: list[str] = []
            stale: list[str] = []
            total = 0
            snapshots: dict[str, tuple[str, int, int] | None] = {}
            identities: dict[
                str, tuple[int, int, int, int, int, int]
            ] = {}
            removal = requested
            for g in model.groups:
                picked = [p for p in g.paths if p in removal]
                survivors = [p for p in g.paths if p not in removal]
                for p in picked:
                    safe = False
                    try:
                        if directory_mode:
                            if p not in snapshots:
                                victim_identity = _trash_identity(os.lstat(p))
                                snapshots[p] = core.snapshot_directory(p)
                                if _trash_identity(os.lstat(p)) != victim_identity:
                                    raise OSError(
                                        errno.ESTALE,
                                        "тека змінилася під час перевірки",
                                        p,
                                    )
                            else:
                                victim_identity = _trash_identity(os.lstat(p))
                            victim_snapshot = snapshots[p]
                            for q in survivors:
                                try:
                                    if q not in snapshots:
                                        snapshots[q] = core.snapshot_directory(q)
                                    if snapshots[q] == victim_snapshot:
                                        safe = True
                                        break
                                except OSError:
                                    snapshots[q] = None
                            if safe and victim_snapshot:
                                total += victim_snapshot[1]
                                identities[p] = victim_identity
                        else:
                            victim_stat = _verified_survivor(res, p, removal)
                            safe = victim_stat is not None
                            if victim_stat is not None:
                                total += res.file_meta[p].size
                                identities[p] = _trash_identity(victim_stat)
                    except OSError:
                        safe = False
                    (victims if safe else stale).append(p)
            return victims, stale, total, identities

        def after_verify(value) -> None:
            victims, stale, total, _identities = value
            self._trashing = False
            if stale:
                QMessageBox.warning(
                    self, "DupScan",
                    f"{len(stale)} елемент(ів) пропущено: повна актуальна "
                    "копія не підтверджена або вміст змінився.")
            if not victims:
                self.status.setText("Нічого безпечно видаляти.")
                return
            q = QMessageBox.question(
                self, "У Кошик?",
                f"Повну ідентичність підтверджено. Перемістити в Кошик "
                f"{len(victims)} елемент(ів)"
                f"{' (~' + human(total) + ')' if total else ''}?\n"
                "У кожній групі лишається незалежна перевірена копія.")
            if q != QMessageBox.StandardButton.Yes:
                return
            self._trashing = True
            self.status.setText(f"Переміщую в Кошик {len(victims)} елемент(ів)…")

            def verify_again_and_trash():
                now_safe, _stale, _total, identities = verify()
                approved = set(victims)
                safe = [p for p in now_safe if p in approved]
                errors = [f"{p}: повторна перевірка не пройдена"
                          for p in victims if p not in safe]
                errors.extend(_to_trash_known(
                    res,
                    safe,
                    expected_identities={
                        path: identities[path]
                        for path in safe
                        if path in identities
                    },
                ))
                return errors

            self._bg_run(verify_again_and_trash,
                         lambda errs: finish(errs, victims), fail)

        def finish(errs: list[str], victims: list[str]) -> None:
            self._trashing = False
            gone = set(victims) - _failed_trash_paths(errs, victims)
            model.checked -= gone
            note = f"У Кошику: {len(gone)}." + (f" Помилок: {len(errs)}." if errs else "")
            self._recompute_async(gone, note)

        def fail(msg: str) -> None:
            self._trashing = False
            self.status.setText(f"Помилка Кошика: {msg}")

        self._bg_run(verify, after_verify, fail)

    # ---- «Перевірити й прибрати» ОДНУ групу зі знімка.
    # Дзеркало пари (_verify_session_pair_then_action /
    # _sim_trash_flow(verified_result=…)): замість НІМОЇ заборони для
    # historical/partial, свіже ПОВНЕ читання рівно членів зачепленої
    # групи — і лише тоді ЗВИЧАЙНИЙ наявний ланцюг Кошика. Джерело доказу
    # змінюється (мінірезультат fresh замість застарілого file_meta
    # знімка); сам ланцюг ReviewDialog → підтвердження →
    # to_trash(expected_identities) → історія — той самий, що для живих
    # груп. self.result НІКОЛИ не мутується.
    def _fresh_group_verify(
            self, relevant_groups: list, requested: set[str],
            directory_mode: bool,
    ) -> tuple[list[str], list[str], int, dict[str, tuple[int, int, int, int, int, int]]]:
        """Один прохід свіжої перевірки МЕМБЕРІВ зачеплених груп — повне
        читання з диска, незалежно від file_meta знімка (§4, D6): старий
        історичний file_meta НЕ використовується як підстава пропустити
        читання. Викликається двічі (як verify() у _delete_duplicate_paths
        для пари) — кожен виклик незалежний, попередній результат на віру
        не береться.

        Для файлової групи: кожен член читається повністю (BLAKE3) у
        МІНІ-ScanResult, збудований ЛИШЕ із щойно прочитаних членів
        зачеплених груп; клас у ньому — щойно обчислений digest, а не той,
        що був записаний у знімку. _verified_survivor на ньому працює як
        завжди — «застарілості» немає за побудовою (усе прочитано щойно).

        Для папкової групи: рекурсивний доказ (core.snapshot_directory) —
        той самий, що вже існує для живих пар, без нових скорочень; жодної
        персистентної «класу» для тек немає, тому доказ парний (жертва
        проти кожного непозначеного члена), як і в живому шляху.
        """
        victims: list[str] = []
        stale: list[str] = []
        total = 0
        identities: dict[str, tuple[int, int, int, int, int, int]] = {}
        removal = requested
        fresh = None
        if not directory_mode:
            touched = {p for g in relevant_groups for p in g.paths}
            fresh = core.ScanResult()
            for p in touched:
                try:
                    digest, st = core.verify_current_file(p, None)
                except OSError:
                    continue
                birthtime_ns = getattr(st, "st_birthtime_ns", None) or int(
                    getattr(st, "st_birthtime", st.st_mtime) * 1e9)
                fresh.file_meta[p] = core.FileInfo(
                    p, st.st_size, st.st_mtime_ns, birthtime_ns,
                    st.st_ctime_ns, st.st_dev, st.st_ino)
                class_id = f"{st.st_size}:{digest}"
                fresh.file_class[p] = class_id
                fresh.class_paths.setdefault(class_id, []).append(p)
        snapshots: dict[str, tuple[str, int, int] | None] = {}
        for g in relevant_groups:
            picked = [p for p in g.paths if p in removal]
            survivors = [p for p in g.paths if p not in removal]
            for p in picked:
                safe = False
                try:
                    if directory_mode:
                        if p not in snapshots:
                            victim_identity = _trash_identity(os.lstat(p))
                            snapshots[p] = core.snapshot_directory(p)
                            if _trash_identity(os.lstat(p)) != victim_identity:
                                raise OSError(
                                    errno.ESTALE,
                                    "тека змінилася під час перевірки", p)
                        else:
                            victim_identity = _trash_identity(os.lstat(p))
                        victim_snapshot = snapshots[p]
                        for q in survivors:
                            try:
                                if q not in snapshots:
                                    snapshots[q] = core.snapshot_directory(q)
                                if snapshots[q] == victim_snapshot:
                                    safe = True
                                    break
                            except OSError:
                                snapshots[q] = None
                        if safe and victim_snapshot:
                            total += victim_snapshot[1]
                            identities[p] = victim_identity
                    else:
                        # fresh гарантовано ScanResult тут —
                        # цю гілку виконано лише коли `not directory_mode`,
                        # той самий інваріант, що встановив fresh на рядку
                        # 5625 (`if not directory_mode:`); mypy не зіставляє
                        # умови двох різних if через проміжний код.
                        assert fresh is not None
                        victim_stat = _verified_survivor(fresh, p, removal)
                        safe = victim_stat is not None
                        if victim_stat is not None:
                            total += fresh.file_meta[p].size
                            identities[p] = _trash_identity(victim_stat)
                except OSError:
                    safe = False
                (victims if safe else stale).append(p)
        return victims, stale, total, identities

    def _verify_snapshot_group_then_trash(
            self, model: GroupModel, requested: set[str]) -> None:
        """Точка входу контекстного меню для historical/partial груп."""
        # Явна відмова, як в delete_checked/
        # _delete_duplicate_paths — ця функція теж кличе model.groups
        # напряму, чого PerceptualModel/ClusterModel не мають.
        if not isinstance(model, GroupModel):
            raise TypeError(
                f"_verify_snapshot_group_then_trash приймає лише "
                f"GroupModel; отримано {type(model).__name__}.")
        res = self.result
        if res is None:
            return
        if self._operation_busy():
            QMessageBox.information(
                self, "DupScan", "Зачекайте завершення поточної операції.")
            return
        if not requested or self._trashing:
            QMessageBox.information(self, "DupScan", "Нічого не позначено.")
            return
        directory_mode = model is self.m_dirs
        relevant_groups = [
            g for g in model.groups if requested.intersection(g.paths)]
        if not relevant_groups:
            return
        for g in relevant_groups:
            if all(p in requested for p in g.paths):
                QMessageBox.warning(
                    self, "DupScan",
                    "У групі мусить лишитися щонайменше один непозначений "
                    "елемент.")
                return
        kind = "тек" if directory_mode else "файлів"
        touched_n = sum(len(g.paths) for g in relevant_groups)
        answer = QMessageBox.question(
            self, "Перевірити й прибрати вибрану групу?",
            "Завантажена сесія є історичним знімком, тому DupScan не буде "
            "використовувати її дані для видалення напряму.\n\n"
            f"Буде повністю перечитано рівно {touched_n} {kind} з "
            f"{len(relevant_groups)} групи(груп) — свіжа перевірка групи. "
            "Збережений знімок не зміниться.\n\n"
            "Перед Кошиком DupScan додатково перечитає жертву й незалежну "
            "копію ще раз. Продовжити?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._trashing = True
        self.status.setText(
            "Перевіряю вибрану групу перед Кошиком (свіжа перевірка групи)…")

        def verify():
            return self._fresh_group_verify(
                relevant_groups, requested, directory_mode)

        def after_verify(value) -> None:
            victims, stale, total, _identities = value
            self._trashing = False
            if stale:
                QMessageBox.warning(
                    self, "DupScan",
                    f"{len(stale)} елемент(ів) пропущено: повна актуальна "
                    "копія не підтверджена або вміст змінився.")
            if not victims:
                self.status.setText("Нічого безпечно видаляти.")
                return
            review_groups = [list(g.paths) for g in relevant_groups]
            reviewed = self._review_selection(
                set(victims), review_groups, directory_mode=directory_mode,
                action_text="Перевірити й прибрати (свіжа перевірка групи)")
            if reviewed is None:
                return
            victims = [p for p in victims if p in reviewed]
            if not victims:
                self.status.setText("Усі позначки знято під час перевірки.")
                return
            q = QMessageBox.question(
                self, "У Кошик?",
                f"Повну ідентичність підтверджено. Перемістити в Кошик "
                f"{len(victims)} елемент(ів)"
                f"{' (~' + human(total) + ')' if total else ''}?\n"
                "У кожній групі лишається незалежна перевірена копія.")
            if q != QMessageBox.StandardButton.Yes:
                return
            self._trashing = True
            self.status.setText(f"Переміщую в Кошик {len(victims)} елемент(ів)…")

            def verify_again_and_trash():
                now_safe, _stale, _total, identities = self._fresh_group_verify(
                    relevant_groups, requested, directory_mode)
                approved = set(victims)
                safe = [p for p in now_safe if p in approved]
                errors = [f"{p}: повторна перевірка не пройдена"
                          for p in victims if p not in safe]
                errors.extend(_to_trash_known(
                    res, safe,
                    expected_identities={
                        path: identities[path]
                        for path in safe if path in identities
                    },
                ))
                return errors

            def finish(errs: list[str]) -> None:
                self._trashing = False
                gone = set(victims) - _failed_trash_paths(errs, victims)
                model.checked -= gone
                for path in gone:
                    model.set_tag(path, "перевірено й у Кошику (свіжа перевірка групи)")
                note = (
                    f"У Кошику: {len(gone)}."
                    + (f" Помилок: {len(errs)}." if errs else "")
                    + " Збережений знімок не змінено.")
                self.status.setText(note)
                # НЕ _recompute_async для historical — знімок незмінний (як у пари).

            def fail(msg: str) -> None:
                self._trashing = False
                self.status.setText(f"Помилка Кошика: {msg}")

            self._bg_run(verify_again_and_trash, finish, fail)

        self._bg_run(verify, after_verify,
                     lambda e: (setattr(self, "_trashing", False),
                                self.status.setText(f"Помилка перевірки: {e}")))

    # ---- вилучення з «Подібності папок»: усе через один потік —
    # фонова верифікація (fresh + жива копія ПОЗА множиною видалення),
    # підтвердження, фоновий Кошик, recompute
    def _sim_trash_flow(
            self, compute_fn, *, verified_result: core.ScanResult | None = None,
            historical_snapshot: bool = False) -> None:
        """Verify and Trash duplicate files from a live or fresh pair result."""
        if verified_result is None:
            if not self._destructive_result_ready() or self._trashing:
                return
            res = self.result
        else:
            if (self._operation_busy() or self._trashing
                    or not verified_result.live or verified_result.partial):
                QMessageBox.information(
                    self, "DupScan",
                    "Свіжа перевірка пари не завершена; файли не змінювались.")
                return
            res = verified_result
        if res is None:
            return
        self._trashing = True
        self.status.setText("Перевіряю файли перед Кошиком…")

        def verify():
            paths = list(compute_fn())
            removal = set(paths)
            victims: list[str] = []
            stale: list[str] = []
            for p in paths:
                try:
                    # safe тепер явний bool, як у сестринських
                    # _fresh_group_verify (5521, 5676) — раніше safe отримував
                    # напряму stat_result | None (truthy-перевірка спрацьовувала,
                    # але "safe = False" у except конфліктувало з типом mypy).
                    victim_stat = _verified_survivor(res, p, removal)
                    safe = victim_stat is not None
                except OSError:
                    safe = False
                (victims if safe else stale).append(p)
            total = sum(res.file_meta[p].size for p in victims
                        if p in res.file_meta)
            return victims, stale, total

        def after_verify(v) -> None:
            victims, stale, total = v
            self._trashing = False
            if stale:
                QMessageBox.warning(
                    self, "DupScan",
                    f"{len(stale)} файл(ів) пропущено: змінились після скану "
                    f"або інша копія недоступна — контент не має зникнути "
                    f"повністю.")
            if not victims:
                self.status.setText("Нічого безпечно видаляти.")
                return
            # file_class.get(path) дає str | None; None-ключ
            # ніколи не збігається зі справжнім class_id, тож .get(None, ())
            # і раніше безпечно давав () — тепер це явно, без покладання на
            # None-як-ключ.
            review_groups = [
                list(res.class_paths.get(class_id, ()))
                if (class_id := res.file_class.get(path)) is not None else []
                for path in victims
            ]
            reviewed = self._review_selection(
                set(victims), review_groups, action_text="Підтвердити вибране",
                result=res)
            if reviewed is None:
                self.status.setText("Операцію скасовано під час перевірки.")
                return
            victims = [path for path in victims if path in reviewed]
            total = sum(res.file_meta[path].size for path in victims
                        if path in res.file_meta)
            if not victims:
                self.status.setText("Усі позначки знято під час перевірки.")
                return
            q = QMessageBox.question(
                self, "У Кошик?",
                f"Перемістити в Кошик {len(victims)} файл(ів)"
                f"{' (~' + human(total) + ')' if total else ''}?\n"
                f"Інша копія кожного файлу лишається. Відновлення — з Кошика.")
            if q != QMessageBox.StandardButton.Yes:
                return
            self._trashing = True
            self.status.setText(f"Переміщую в Кошик {len(victims)} елемент(ів)…")

            def ok(errs: list[str]) -> None:
                self._trashing = False
                gone = set(victims) - _failed_trash_paths(errs, victims)
                self.m_sim.checked -= gone
                note = f"У Кошику: {len(gone)}." + (
                    f" Помилок: {len(errs)}." if errs else "")
                if historical_snapshot:
                    self.status.setText(
                        note
                        + " Пару було перевірено заново; історичну сесію "
                        "не змінено. Повторіть сканування для оновлення.")
                else:
                    self._recompute_async(gone, note)

            def err(msg: str) -> None:
                self._trashing = False
                self.status.setText(f"Помилка Кошика: {msg}")

            self._bg_run(lambda: _verified_to_trash_files(res, victims), ok, err)

        self._bg_run(verify, after_verify,
                     lambda e: (setattr(self, "_trashing", False),
                                self.status.setText(f"Помилка перевірки: {e}")))

    def delete_checked_sim(self) -> None:
        if self.result is None or not self.m_sim.checked:
            QMessageBox.information(self, "DupScan", "Нічого не позначено.")
            return
        checked = sorted(self.m_sim.checked)
        self._sim_trash_flow(lambda: checked)

    def _sim_remove_shared(self, pr, remove_from_a: bool) -> None:
        """Масове: увесь спільний вміст обраного боку пари — у Кошик."""
        res = self.result
        if res is None:
            return
        # Тека-еталон: бік-еталон не спустошується.
        side_dir = pr.dir_a if remove_from_a else pr.dir_b
        if self._reference_blocked(side_dir):
            self.status.setText(
                "Дію зупинено: обраний бік є текою-еталоном "
                f"(недоторканна): {side_dir}")
            return
        if res.live and not res.partial:
            self._sim_trash_flow(
                lambda: core.shared_side(
                    res, pr.dir_a, pr.dir_b, remove_from_a))
            return
        side = "A" if remove_from_a else "B"

        def resolved(current) -> None:
            def verified(fresh) -> None:
                self._sim_trash_flow(
                    lambda: core.shared_side(
                        fresh, current.dir_a, current.dir_b, remove_from_a),
                    verified_result=fresh,
                    historical_snapshot=True,
                )

            self._verify_session_pair_then_action(
                current,
                original_pr=pr,
                action_text=f"перемістити спільні файли з боку {side} у Кошик",
                on_verified=verified,
            )

        self._with_resolved_pair(
            pr,
            purpose=f"вибіркова перевірка спільних файлів боку {side}",
            on_ready=resolved,
        )

    # ---- ігнорування пар (живе в result, переживає сесії)
    def _sim_ignore(self, pr) -> None:
        if self.result is None:
            return
        self.result.ignored_pairs.add((pr.dir_a, pr.dir_b))
        self.m_sim.set_pairs([p for p in self.m_sim.pairs if p is not pr])
        self._update_ignored_btn()
        self.status.setText(
            "Пару приховано. «Скинути ігнорування» на вкладці поверне всі.")

    def _sim_reset_ignored(self) -> None:
        res = self.result
        if res is None or not res.ignored_pairs:
            return
        res.ignored_pairs.clear()
        self.status.setText("Повертаю проігноровані пари…")
        self._bg_run(lambda: (core._aggregate(res), None)[1],
                     lambda _v: (self._refresh_models(),
                                 self._update_ignored_btn()),
                     lambda e: self.status.setText(f"Помилка: {e}"))

    def _update_ignored_btn(self) -> None:
        n = len(self.result.ignored_pairs) if self.result else 0
        self.b_ignored.setVisible(n > 0)
        self.b_ignored.setText(f"Скинути ігнорування ({n})")

    def _sim_expanded(self, pidx) -> None:
        """Розгортання пари: дорахувати «лише в A / лише в B» у фоні (один раз)."""
        src = pidx
        if not src.isValid() or src.internalId() != 0 or self.result is None:
            return
        pi = self.m_sim.root_id(src.row())
        if pi is None:
            return
        if pi in self.m_sim.diff or not (0 <= pi < len(self.m_sim.pairs)):
            return
        pr = self.m_sim.pairs[pi]
        self.m_sim.diff[pi] = None  # сентинел: уже рахується
        res = self.result

        def ok(v, pi=pi, pr=pr) -> None:
            # результат міг застаріти (recompute перебудував пари) — звіряємо
            if pi < len(self.m_sim.pairs) and self.m_sim.pairs[pi] is pr:
                self.m_sim.set_diff(pi, *v)
            else:
                self.m_sim.diff.pop(pi, None)

        self._bg_run(lambda: core.pair_diff(res, pr.dir_a, pr.dir_b), ok,
                     lambda e, pi=pi: self.m_sim.diff.pop(pi, None))

    def _sim_menu(self, pos, *, execute: bool = True) -> QMenu | None:
        # mypy: `return` без значення у функції з `-> X | None`
        # ловиться як "Return value expected" саме в цій версії mypy —
        # явний `return None` (поведінково ідентичний) це знімає.
        idx = self.v_sim.indexAt(pos)
        if not idx.isValid():
            return None
        self.v_sim.setCurrentIndex(idx)
        self._update_action_state()
        src = idx
        pi = (self.m_sim.root_id(src.row()) if src.internalId() == 0
              else int(src.internalId()) - 1)
        if pi is None:
            return None
        if not (0 <= pi < len(self.m_sim.pairs)):
            return None
        pr = self.m_sim.pairs[pi]
        menu = QMenu(self)

        def pair_action(text: str, callback):
            action = menu.addAction(text, callback)
            action.setToolTip(f"Тека A:\n{pr.dir_a}\n\nТека B:\n{pr.dir_b}")
            return action

        path = self.m_sim.path_at(idx) if idx.internalId() != 0 else None
        checkable = bool(self.m_sim.flags(idx) & Qt.ItemIsUserCheckable)
        if path:
            if checkable:
                menu.addAction(self.action_trash_current)
                menu.addAction(self.action_toggle_trash_mark)
                if self.m_sim.checked:
                    menu.addAction(self.action_trash_marked)
                menu.addSeparator()
            menu.addAction(
                "Quick Look", lambda p=path: self._open_result_path(p, quick_look))
            menu.addAction(
                "Показати у Finder", lambda p=path: self._open_result_path(p, reveal))
            menu.addSeparator()
        needs_pair_check = bool(self.result and (
            not self.result.live or self.result.partial))
        remove_prefix = "Перевірити й прибрати" if needs_pair_check else "Прибрати"
        pair_action(f"{remove_prefix} спільне з A → Кошик",
                    lambda: self._sim_remove_shared(pr, True))
        pair_action(f"{remove_prefix} спільне з B → Кошик",
                    lambda: self._sim_remove_shared(pr, False))
        menu.addSeparator()
        pair_action(
            "Показати теку A у Finder",
            lambda: self._open_result_path(pr.dir_a, reveal))
        pair_action(
            "Показати теку B у Finder",
            lambda: self._open_result_path(pr.dir_b, reveal))
        menu.addSeparator()
        merge_prefix = "Перевірити й злити" if needs_pair_check else "Злити"
        pair_action(f"{merge_prefix} B → A…",
                    lambda: self._sim_merge(pr, into_a=True))
        pair_action(f"{merge_prefix} A → B…",
                    lambda: self._sim_merge(pr, into_a=False))
        menu.addSeparator()
        tag_menu = menu.addMenu("Мітка")
        for tag in ("До перевірки", "Схвалено", "Відкласти"):
            tag_menu.addAction(tag, lambda _checked=False, value=tag:
                               self._set_pair_tag(pr, value))
        tag_menu.addAction("Без мітки", lambda: self._set_pair_tag(pr, ""))
        menu.addSeparator()
        menu.addAction("Ігнорувати пару", lambda: self._sim_ignore(pr))
        if execute:
            menu.exec(self.v_sim.viewport().mapToGlobal(pos))
        return menu

    def _set_pair_tag(self, pair, tag: str) -> None:
        self.m_sim.set_tag(pair, tag)
        self._update_review_queue_button()
        encoded = {"\n".join(key): value for key, value in self.m_sim.tags.items()}
        self._settings.setValue(
            "similarity/tags", json.dumps(encoded, ensure_ascii=False, sort_keys=True))
        self.status.setText(
            f"Мітку «{tag}» застосовано." if tag else "Мітку пари знято.")

    # ---- злиття пари: унікальне з джерела -> у ціль, потім джерело в Кошик
    def _with_resolved_pair(self, pr, *, purpose: str, on_ready) -> None:
        def ready(paths: tuple[str, ...]) -> None:
            current = core.SimPair(
                paths[0], paths[1], pr.percent, pr.shared_bytes,
                shared_total=pr.shared_total,
            )
            on_ready(current)

        self._resolve_session_paths(
            (pr.dir_a, pr.dir_b), purpose=purpose, on_ready=ready,
            expect_directories=True,
        )

    def _sim_merge(self, pr, into_a: bool) -> None:
        """Merge a live pair, or freshly verify only A/B from a saved session."""
        res = self.result
        if res is None or self._merging or self._trashing:
            return
        # Тека-еталон: злиття СПУСТОШУЄ джерело (перенос +
        # можливий Кошик теки) — з еталона заборонено. Ціль-еталон дозволена:
        # докладати в еталон безпечно.
        merge_source = pr.dir_b if into_a else pr.dir_a
        if self._reference_blocked(merge_source):
            self.status.setText(
                "Злиття зупинено: тека-джерело є текою-еталоном "
                f"(недоторканна): {merge_source}")
            return
        if res.live and not res.partial:
            self._run_sim_merge(pr, into_a, res)
            return
        direction = "B → A" if into_a else "A → B"
        self._with_resolved_pair(
            pr, purpose=f"вибіркова перевірка і злиття {direction}",
            on_ready=lambda current: self._verify_session_pair_then_merge(
                current, into_a, original_pr=pr),
        )

    def _verify_session_pair_then_merge(
            self, pr, into_a: bool, *, original_pr=None) -> None:
        """Freshly prove one current A/B pair, then prepare only its merge."""
        src_dir, dst_dir = ((pr.dir_b, pr.dir_a) if into_a
                            else (pr.dir_a, pr.dir_b))
        source_side, target_side = ("B", "A") if into_a else ("A", "B")
        original_pr = original_pr or pr

        def verified(fresh) -> None:
            self.status.setText("Пару A/B перевірено заново. Готую план злиття…")
            self._run_sim_merge(
                pr, into_a, fresh, pair_verified=True,
            )

        self._verify_session_pair_then_action(
            pr,
            original_pr=original_pr,
            action_text=(
                f"злити {source_side} → {target_side}\n"
                f"{src_dir}\n→\n{dst_dir}"
            ),
            on_verified=verified,
        )

    def _verify_session_pair_then_action(
            self, pr, *, original_pr, action_text: str, on_verified) -> None:
        """Build a fresh proof for exactly one historical/current A/B pair."""
        if self._operation_busy():
            QMessageBox.information(
                self, "DupScan", "Зачекайте завершення поточної операції.")
            return
        moved = (
            original_pr.dir_a != pr.dir_a or original_pr.dir_b != pr.dir_b
        )
        current_paths = (
            f"\n\nПоточна тека A:\n{pr.dir_a}\n\n"
            f"Поточна тека B:\n{pr.dir_b}"
            if moved else ""
        )
        answer = QMessageBox.question(
            self, "Перевірити вибрані теки перед дією?",
            "Завантажена сесія є історичним знімком, тому DupScan не буде "
            "використовувати її дані для переміщення файлів.\n\n"
            f"Тека A у сесії:\n{original_pr.dir_a}\n\n"
            f"Тека B у сесії:\n{original_pr.dir_b}"
            f"{current_paths}\n\n"
            f"Запитана дія:\n{action_text}\n\n"
            "Буде проскановано й перевірено лише ці дві теки. Решта "
            "початкових коренів не сканується. Перед переносом і перед "
            "Кошиком DupScan додатково перечитає потрібні файли. Продовжити?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._progress = None
        self._eta.reset()
        self.b_scan.setEnabled(False)
        self.b_pause.setEnabled(True)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self._set_operation_controls_visible(True)
        self.status.setText("Перевіряю лише вибрану пару A/B перед дією…")
        self._set_results_operation(
            "Перевіряю лише вибрані теки A/B. Файли ще не змінюються; "
            "перевірку можна призупинити або скасувати.",
            visible=True,
        )
        worker = PairVerificationWorker(pr.dir_a, pr.dir_b)
        self.pair_worker = worker
        context_token = self._session_context_token
        operation_result = self.result

        def active() -> bool:
            return (
                self.pair_worker is worker
                and context_token == self._session_context_token
                and operation_result is self.result
            )

        def progress(phase: str, done: int, total: int) -> None:
            if active():
                self.on_progress(phase, done, total)

        worker.progress.connect(progress)

        def restore_controls() -> None:
            self.b_scan.setEnabled(True)
            self.b_pause.setEnabled(False)
            self.b_pause.setText("Пауза")
            self.b_cancel.setEnabled(False)
            self.bar.setRange(0, 1)
            self.bar.setValue(0)
            self._set_operation_controls_visible(False)
            self._set_results_operation(visible=False)

        def verified(fresh, worker=worker) -> None:
            if not active():
                if self.pair_worker is worker:
                    self.pair_worker = None
                return
            self.pair_worker = None
            if self._closing:
                return
            restore_controls()
            if worker.cancel.is_set() or fresh.partial:
                self.status.setText(
                    "Перевірку вибраної пари скасовано. Жодного файла не змінено.")
                return
            incomplete = not (
                core.directory_tree_mergeable(fresh, pr.dir_a)
                and core.directory_tree_mergeable(fresh, pr.dir_b))
            if incomplete:
                details = "\n".join(fresh.errors[:5])
                if not details:
                    failed_dirs = sorted(fresh.dir_read_failed)[:5]
                    if failed_dirs:
                        details = "Теки зі збоєм читання:\n" + "\n".join(
                            failed_dirs)
                    else:
                        details = (
                            "принаймні одна тека або її дочірній елемент не "
                            "має повного підтвердженого обходу"
                        )
                QMessageBox.warning(
                    self, "Не вдалося безпечно перевірити пару",
                    "Не всі елементи у вибраних теках вдалося прочитати.\n\n"
                    f"Тека A у сесії:\n{original_pr.dir_a}\n\n"
                    f"Тека B у сесії:\n{original_pr.dir_b}\n\n"
                    f"Перевірена поточна тека A:\n{pr.dir_a}\n\n"
                    f"Перевірена поточна тека B:\n{pr.dir_b}\n\n"
                    f"Причина:\n{details}\n\n"
                    "Дію скасовано; виправте доступ або помилки носія й "
                    "повторіть перевірку лише цієї пари. Якщо структура "
                    "диска змінилась, скористайтеся «Оновити дані…» або "
                    "повторно оберіть поточний корінь.")
                return
            on_verified(fresh)

        def failed(message: str, worker=worker) -> None:
            if not active():
                if self.pair_worker is worker:
                    self.pair_worker = None
                return
            self.pair_worker = None
            if self._closing:
                return
            restore_controls()
            QMessageBox.warning(
                self, "Помилка перевірки вибраної пари",
                "Запитана дія не починалась.\n\n"
                f"Тека A у сесії:\n{original_pr.dir_a}\n\n"
                f"Тека B у сесії:\n{original_pr.dir_b}\n\n"
                f"Перевірена поточна тека A:\n{pr.dir_a}\n\n"
                f"Перевірена поточна тека B:\n{pr.dir_b}\n\n"
                f"Причина:\n{message}\n\n"
                "Якщо диск або теки переміщено, скористайтеся "
                "«Оновити дані…» або повторно оберіть поточний корінь.")
            self.status.setText("Перевірка вибраної пари не завершилась.")

        worker.done.connect(verified)
        worker.failed.connect(failed)
        worker.start()

    def _sim_merge_active(self, op: _SimMergeOp, worker) -> bool:
        return (
            self.merge_worker is worker
            and op.context_token == self._session_context_token
            and op.operation_result is self.result
        )

    def _sim_merge_clear_worker(self, op: _SimMergeOp, current) -> None:
        if self.merge_worker is current:
            self.merge_worker = None
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_operation_controls_visible(False)

    def _sim_merge_start_worker(self, op: _SimMergeOp, current, on_done, on_error,
                                text: str) -> None:
        self.merge_worker = current
        self._progress = None
        self._eta.reset()
        self.b_pause.setEnabled(True)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self._set_operation_controls_visible(True)
        self._set_results_operation(text, visible=True)

        def managed_progress(phase: str, done: int, total: int) -> None:
            if self._sim_merge_active(op, current):
                self.on_progress(phase, done, total)

        def managed_done(value) -> None:
            if not self._sim_merge_active(op, current):
                if self.merge_worker is current:
                    self.merge_worker = None
                return
            self._sim_merge_clear_worker(op, current)
            if not self._closing:
                on_done(value)

        def managed_failed(message: str) -> None:
            if not self._sim_merge_active(op, current):
                if self.merge_worker is current:
                    self.merge_worker = None
                return
            self._sim_merge_clear_worker(op, current)
            if not self._closing:
                on_error(message)

        current.progress.connect(managed_progress)
        current.done.connect(managed_done)
        current.failed.connect(managed_failed)
        current.start()

    def _sim_merge_finish(self, op: _SimMergeOp, note: str) -> None:
        self._merging = False
        self._set_results_operation(visible=False)
        removed = {src for src, _dst in op.ctx["moved"]}
        if op.ctx.get("trashed_dir"):
            removed = {op.src_dir}
        if removed:
            if op.pair_verified:
                # Never mutate the loaded snapshot. The fresh pair proof
                # is deliberately local to this operation; another action
                # will verify A/B again from disk.
                self.status.setText(
                    note
                    + " Дані цієї пари перевірені заново. Історичну "
                    "сесію не змінено; повторіть сканування для оновлення.")
            else:
                self._recompute_async(
                    removed,
                    note + " Запустіть «Сканувати», щоб побачити перенесене.")
        else:
            self.status.setText(note)

    def _sim_merge_on_trash_step(self, op: _SimMergeOp, v) -> None:
        kind, payload = v
        if kind == "cancelled":
            self._sim_merge_finish(
                op,
                "Перевірку перед Кошиком скасовано; завершені переноси "
                "лишилися, тека-джерело не переміщена в Кошик.")
            return
        if kind == "abort":
            # ЯКІ саме файли без доказу — з воркера (перші 10 у діалозі,
            # повний список — у файл). Голе число змушувало власника
            # вгадувати (біль першого дня: «20 шляхів не прочитано»).
            # merge_worker на цей момент уже очищений менеджером воркерів,
            # тому список їде через ctx (канал, яким уже їдуть moved/
            # survivors), а не через self.
            gate_worker = op.ctx.get("trash_worker")
            unproven = list(getattr(gate_worker, "unproven", []) or [])
            sample = "\n".join(unproven[:10])
            if len(unproven) > 10:
                sample += f"\n… і ще {len(unproven) - 10}"
            if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
                # Тести мокають QMessageBox.warning, не конструктор:
                # сирий exec() тут ВІШАВ увесь прогін (та сама пастка,
                # що ловилась раніше). Кнопка звіту —
                # лише в живому вікні.
                QMessageBox.warning(
                    self, "DupScan",
                    f"Теку-джерело ({op.source_side}) НЕ переміщено в "
                    f"Кошик: {payload} файл(ів) без живої копії поза "
                    f"нею.\n\n"
                    + (f"Без доказу:\n{sample}\n\n" if sample else "")
                    + f"Джерело:\n{op.src_dir}\n\nЦіль:\n{op.dst_dir}")
                self._sim_merge_finish(op, "Злиття часткове: тека лишилась.")
                return
            box = QMessageBox(self)
            box.setWindowTitle("DupScan")
            box.setIcon(QMessageBox.Icon.Warning)
            box.setText(
                f"Теку-джерело ({op.source_side}) НЕ переміщено в Кошик: "
                f"{payload} файл(ів) без живої копії поза нею.\n\n"
                + (f"Без доказу:\n{sample}\n\n" if sample else "")
                + f"Джерело:\n{op.src_dir}\n\nЦіль:\n{op.dst_dir}")
            save_button = (
                box.addButton("Зберегти повний список…",
                              QMessageBox.ButtonRole.ActionRole)
                if unproven else None)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if save_button is not None and box.clickedButton() is save_button:
                self._export_merge_plan(
                    [(0, path, os.path.basename(path)) for path in unproven],
                    op.src_dir, op.dst_dir)
            self._sim_merge_finish(op, "Злиття часткове: тека лишилась.")
            return
        errs = payload
        op.ctx["trashed_dir"] = not errs
        if not errs:
            # Знімок незмінний, але інтерфейс не має мовчати: рядок пари
            # отримує мітку у view-шарі. Live-результат після recompute
            # перебудує модель — там мітка зникне разом із парою.
            self.m_sim.set_tag(op.pr, f"злито · джерело ({op.source_side}) в Кошику")
        self._sim_merge_finish(
            op,
            "Злито: теку переміщено в Кошик." if not errs
            else f"Злиття: Кошик з помилками ({len(errs)}).")

    def _sim_merge_on_moved(self, op: _SimMergeOp, v) -> None:
        moved, errors = v[:2]
        cancelled = bool(v[2]) if len(v) > 2 else False
        skipped_pairs = list(v[3]) if len(v) > 3 else []
        skipped = len(skipped_pairs)
        # Доказані під час злиття копії для файлів без класу в скані:
        # без них гейт Кошика відхилив би кожен неохоплений файл, що
        # лишився в джерелі саме тому, що ідентичний уже є в цілі.
        op.ctx["survivors"] = dict(skipped_pairs)
        op.ctx["moved"] = moved
        self._set_results_operation(visible=False)
        if cancelled:
            self._sim_merge_finish(
                op,
                f"Перенос зупинено безпечно після {len(moved)} файл(ів). "
                "Вже завершені файлові переноси не відкочувались; "
                "теку-джерело не переміщено в Кошик.")
            return
        if errors:
            QMessageBox.warning(
                self, "DupScan",
                _move_result_message(len(moved), errors))
            self._sim_merge_finish(
                op,
                f"Злиття часткове: перенесено {len(moved)} файл(ів), "
                f"не перенесено {len(errors)}. Теку-джерело лишено.")
            return
        q = QMessageBox.question(
            self, "Тека в Кошик?",
            f"Напрямок злиття: {op.source_side} → {op.target_side}\n\n"
            f"Тека-джерело ({op.source_side}), яку буде переміщено в Кошик:\n"
            f"{op.src_dir}\n\n"
            f"Тека призначення ({op.target_side}), яка залишиться:\n"
            f"{op.dst_dir}\n\n"
            + f"Перенесено файлів: {len(moved)}"
            + (f"; пропущено {skipped} — той самий вміст уже був у цілі."
               if skipped else ".")
            + "\n\n"
            + ("Переносити не було чого: весь вміст джерела вже є в "
               "цілі.\n\n" if not moved and skipped else "")
            + "Джерело тепер містить лише спільний вміст. "
            "Перемістити всю теку-джерело в Кошик?\n"
            "Перед цим кожен лишковий файл буде перевірено на живу "
            "копію поза текою.")
        if q != QMessageBox.StandardButton.Yes:
            self._sim_merge_finish(
                op,
                f"Перенесено {len(moved)} файл(ів); теку-джерело "
                f"({op.source_side}) лишено на місці.")
            return
        self.status.setText(
            f"Перевіряю лишки джерела {op.source_side} перед Кошиком…")
        trash_worker = DirectoryTrashWorker(
            op.res, op.src_dir, op.ctx.get("survivors"))
        op.ctx["trash_worker"] = trash_worker
        self._sim_merge_start_worker(
            op, trash_worker,
            lambda v: self._sim_merge_on_trash_step(op, v),
            lambda error: self._sim_merge_finish(op, f"Помилка перевірки: {error}"),
            "Повторно перевіряю всю теку-джерело перед Кошиком. "
            "Можна призупинити або скасувати до системної дії.",
        )

    def _sim_merge_on_copied(self, op: _SimMergeOp, v) -> None:
        copied, errors = v[:2]
        cancelled = bool(v[2]) if len(v) > 2 else False
        skipped = len(v[3]) if len(v) > 3 else 0
        self._merging = False
        self._set_results_operation(visible=False)
        if errors:
            QMessageBox.warning(
                self, "DupScan",
                f"Скопійовано {len(copied)} файл(ів); помилок: {len(errors)}. "
                f"Першу помилку: {errors[0]}")
        if cancelled:
            self.status.setText(
                f"Копіювання зупинено безпечно після {len(copied)} "
                "файл(ів). Джерело не змінено; завершені копії лишились.")
            return
        self.status.setText(
            f"Скопійовано й перевірено {len(copied)} файл(ів). "
            + (f"Пропущено {skipped}: той самий вміст уже був у цілі. "
               if skipped else "")
            + "Джерело не змінено."
            + (" Історичну сесію не змінено." if op.pair_verified
               else " Повторіть сканування для оновлення результатів."))

    def _sim_merge_on_plan(self, op: _SimMergeOp, v) -> None:
        plan, total, expected_digests, same_device, root_identities = v
        if not plan:
            self._sim_merge_on_moved(op, ([], []))
            return
        preview = "\n".join(rel for _s, _p, rel in plan[:15])
        if len(plan) > 15:
            preview += f"\n… і ще {len(plan) - 15}"
        mode = "move"
        message = (
            f"Унікальних файлів: {len(plan)} (~{human(total)})\n"
            f"Джерело ({op.source_side}):\n{op.src_dir}\n\n"
            f"Ціль ({op.target_side}):\n{op.dst_dir}\n\n"
            f"Буде перенесено/скопійовано:\n{preview}\n\n"
            "Копіювання лишає джерело без змін. Перенос після цього "
            "запропонує перемістити залишок джерельної теки в Кошик.")
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            accepted = QMessageBox.question(
                self, "Злити?", message) == QMessageBox.StandardButton.Yes
        else:
            # Кнопка звіту повертає до цього ж діалогу (цикл): збереження
            # плану — не рішення, діалог мусить пережити його.
            while True:
                choice = QMessageBox(self)
                choice.setWindowTitle("Злити теки")
                choice.setIcon(QMessageBox.Icon.Question)
                choice.setText(message)
                copy_button = choice.addButton(
                    "Копіювати й перевірити", QMessageBox.ButtonRole.AcceptRole)
                move_button = choice.addButton(
                    "Перенести", QMessageBox.ButtonRole.DestructiveRole)
                export_button = choice.addButton(
                    "Зберегти повний план…", QMessageBox.ButtonRole.ActionRole)
                choice.addButton(QMessageBox.StandardButton.Cancel)
                choice.exec()
                if choice.clickedButton() is export_button:
                    self._export_merge_plan(plan, op.src_dir, op.dst_dir)
                    continue
                break
            accepted = choice.clickedButton() in (copy_button, move_button)
            mode = "copy" if choice.clickedButton() is copy_button else "move"
        if not accepted:
            self._merging = False
            self.status.setText("Злиття скасовано.")
            return
        if mode == "copy":
            self.status.setText(f"Копіюю й перевіряю {len(plan)} файл(ів)…")
            transfer = MergeTransferWorker(
                "copy", op.res, plan, op.dst_dir, op.src_dir, expected_digests,
                root_identities)
            self._sim_merge_start_worker(
                op, transfer,
                lambda v: self._sim_merge_on_copied(op, v),
                lambda error: self._sim_merge_finish(op, f"Помилка копіювання: {error}"),
                f"Копіюю й перевіряю {len(plan)} файл(ів). "
                "Скасування прибере незавершений temp; готові копії "
                "лишаться, джерело не зміниться.",
            )
            return
        if not same_device:
            self._merging = False
            self._set_results_operation(visible=False)
            QMessageBox.information(
                self, "DupScan",
                "Перенос між томами недоступний. Оберіть безпечне копіювання.")
            return
        self.status.setText(f"Переношу {len(plan)} файл(ів)…")
        transfer = MergeTransferWorker(
            "move", op.res, plan, op.dst_dir, op.src_dir, expected_digests,
            root_identities)
        self._sim_merge_start_worker(
            op, transfer,
            lambda v: self._sim_merge_on_moved(op, v),
            lambda error: self._sim_merge_finish(op, f"Помилка переносу: {error}"),
            f"Переношу {len(plan)} файл(ів) {op.source_side} → "
            f"{op.target_side}. Скасування зупинить перед наступною "
            "атомарною файловою дією; завершені переноси лишаться.",
        )

    def _sim_merge_prepared(self, op: _SimMergeOp, worker, value) -> None:
        if not self._sim_merge_active(op, worker):
            if self.merge_worker is worker:
                self.merge_worker = None
            return
        self.merge_worker = None
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_operation_controls_visible(False)
        self._set_results_operation(visible=False)
        if self._closing:
            self._merging = False
            return
        if value is None or (worker and worker.cancel.is_set()):
            self._merging = False
            self.status.setText(
                "Підготовку злиття скасовано. Жодного файла не змінено.")
            return
        self._sim_merge_on_plan(op, value)

    def _sim_merge_preparation_failed(self, op: _SimMergeOp, worker, message: str) -> None:
        if not self._sim_merge_active(op, worker):
            if self.merge_worker is worker:
                self.merge_worker = None
            return
        self.merge_worker = None
        self.b_pause.setEnabled(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self._set_operation_controls_visible(False)
        self._set_results_operation(visible=False)
        self._sim_merge_finish(op, f"Помилка плану злиття: {message}")

    def _run_sim_merge(self, pr, into_a: bool, res: core.ScanResult,
                       *, pair_verified: bool = False,
                       already_merging: bool = False) -> None:
        if self._trashing or (self._merging and not already_merging):
            return
        src_dir, dst_dir = ((pr.dir_b, pr.dir_a) if into_a
                            else (pr.dir_a, pr.dir_b))
        source_side, target_side = ("B", "A") if into_a else ("A", "B")
        if not already_merging:
            self._merging = True
        self.status.setText(f"Готую злиття {source_side} → {target_side}…")
        op = _SimMergeOp(
            pr=pr, into_a=into_a, res=res, pair_verified=pair_verified,
            src_dir=src_dir, dst_dir=dst_dir, source_side=source_side,
            target_side=target_side, context_token=self._session_context_token,
            operation_result=self.result,
        )
        self._progress = None
        self._eta.reset()
        self.b_pause.setEnabled(True)
        self.b_pause.setText("Пауза")
        self.b_cancel.setEnabled(True)
        self.bar.setRange(0, 0)
        self._set_operation_controls_visible(True)
        self._set_results_operation(
            f"Готую безпечне злиття {source_side} → {target_side}: "
            "перечитую унікальні файли повністю. Файли ще не змінюються; "
            "можна призупинити або скасувати.",
            visible=True,
        )
        worker = MergePreparationWorker(res, src_dir, dst_dir)
        self.merge_worker = worker

        def progress(phase: str, done: int, total: int) -> None:
            if self._sim_merge_active(op, worker):
                self.on_progress(phase, done, total)

        worker.progress.connect(progress)
        worker.done.connect(lambda value: self._sim_merge_prepared(op, worker, value))
        worker.failed.connect(
            lambda message: self._sim_merge_preparation_failed(op, worker, message))
        worker.start()

    def _running_workers(self) -> list[QThread]:
        workers: list[QThread] = []
        if self.worker and self.worker.isRunning():
            workers.append(self.worker)
        if self.session_load_worker and self.session_load_worker.isRunning():
            workers.append(self.session_load_worker)
        if self.refresh_worker and self.refresh_worker.isRunning():
            workers.append(self.refresh_worker)
        if self.pair_worker and self.pair_worker.isRunning():
            workers.append(self.pair_worker)
        if self.merge_worker and self.merge_worker.isRunning():
            workers.append(self.merge_worker)
        if self._rw and self._rw.isRunning():
            workers.append(self._rw)
        workers.extend(w for w in (*self._bg, *self._ui_bg) if w.isRunning())
        return workers

    def _poll_shutdown(self) -> None:
        running = self._running_workers()
        if running:
            elapsed = time.monotonic() - self._shutdown_started
            self.status.setText(
                f"Безпечно завершую {len(running)} фонових операцій… "
                f"{elapsed:.1f} с"
            )
            return
        self._shutdown_timer.stop()
        self._close_ready = True
        self.close()

    # ---- вихід: жодного wait() у GUI; закрити лише після worker-ів
    def closeEvent(self, event) -> None:
        if self._close_ready:
            event.accept()
            return
        if not self._closing:
            self._closing = True
            self._batch_selection_token += 1
            self._batch_selecting = False
            self._model_prepare_token += 1
            self._model_preparing = False
            if self._model_prepare_cancel is not None:
                self._model_prepare_cancel.set()
            if self._refresh_preflight_cancel is not None:
                self._refresh_preflight_cancel.set()
            self._shutdown_started = time.monotonic()
            self._save_view_settings()
            if self.worker and self.worker.isRunning():
                self.worker.cancel.set()
                self.worker.pause.clear()
            if (
                self.session_load_worker
                and self.session_load_worker.isRunning()
            ):
                self.session_load_worker.cancel.set()
            if self.refresh_worker and self.refresh_worker.isRunning():
                self.refresh_worker.cancel.set()
                self.refresh_worker.pause.clear()
            if self.pair_worker and self.pair_worker.isRunning():
                self.pair_worker.cancel.set()
                self.pair_worker.pause.clear()
            if self.merge_worker and self.merge_worker.isRunning():
                self.merge_worker.cancel.set()
                self.merge_worker.pause.clear()
            if self._rw and self._rw.isRunning():
                self._rw.cancel.set()
            for cancel in self._filter_cancel.values():
                cancel.set()
            self.centralWidget().setEnabled(False)
        if self._running_workers():
            event.ignore()
            self.status.setText("Безпечно завершую фонові операції…")
            if not self._shutdown_timer.isActive():
                self._shutdown_timer.start()
            return
        self._close_ready = True
        event.accept()


def main() -> None:
    if "--smoke" in sys.argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    install_crash_handler()
    app = QApplication(sys.argv)
    w = Main()
    w.show()
    if "--smoke" in sys.argv:
        # Constructor-only smoke misses broken Qt plugins and queued startup
        # callbacks. Pump a bounded real event loop in the frozen bundle.
        QTimer.singleShot(200, app.quit)
        code = app.exec()
        print("smoke-ok")
        if code:
            raise SystemExit(code)
        return
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
