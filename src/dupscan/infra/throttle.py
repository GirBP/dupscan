"""Адаптивне дроселювання фізичного читання під час скану + best-effort
температура диска.

Адаптовано з DupFinder src/dupfinder/throttle.py.
Перенесено ``DiskActivity``, ``AdaptiveConcurrency``
(явно названі ТЗ) і температурний ланцюг (``read_disk_temperature``,
``TemperatureGovernor``) — після перевірки джерела: температура читається
через зовнішній ``smartctl`` (smartmontools) командою ``subprocess``, це
НЕ приватний/sudo-API — емпірично підтверджено (`smartctl -A /dev/disk0`
без sudo, звичайний обліковий запис, реальна температура). Best-effort за
побудовою і в джерелі: `smartctl` відсутній/без прав/без SMART-проходу
через USB-міст → ``None``, виклики це вже обробляють. Не портовано:
``Throttle`` (агрегатний duty-cycle-лімітер за load_percent) — ТЗ Блоку T
називає лише AdaptiveConcurrency/media_type, окремий load-percent-регулятор
поза скопом.

DupScan — macOS-only: Windows-гілки (``os.name == "nt"``) і Linux-регекси
пристрою (``nvme``/``mmcblk``/``sd``) з джерела не перенесені.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import time
from threading import Condition, Event, Lock

logger = logging.getLogger(__name__)

# Скільки градусів диск мусить охолонути нижче межі, перш ніж пауза на
# перегрів знімається — щоб не смикати паузу/відновлення рівно на межі.
_HYSTERESIS = 3.0
_POLL_SECONDS = 5.0
# Читання, що було успішним і раптом стало недоступним (нестабільний USB-
# міст), не має миттєво знімати паузу-охолодження; здатися лише після
# стількох поспіль пропусків.
_MAX_TRANSIENT_MISSES = 3
_SMARTCTL_TIMEOUT = 4
_DEVICE_TYPES: tuple[str | None, ...] = (None, "sat", "usbjmicron", "auto")
_DEVICE_DTYPE_CACHE: dict[str, str | None] = {}


class DiskActivity:
    """Потокобезпечний акумулятор прочитаних байтів і wall-clock busy-часу.

    Кожен хеш-потік бере читання в дужки start_read/end_read. Busy-час —
    wall-clock ОБ'ЄДНАННЯ всіх одночасних читань (диск "зайнятий", коли
    читає ХОЧА Б один потік), а не сума per-thread часів, яка при N
    паралельних читачах сягала б N× wall-часу.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self.bytes = 0
        self._active = 0
        self._busy_since: float | None = None
        self._busy_wall = 0.0

    def start_read(self, now: float) -> None:
        with self._lock:
            if self._active == 0:
                self._busy_since = now
            self._active += 1

    def end_read(self, now: float, n_bytes: int) -> None:
        with self._lock:
            if n_bytes > 0:
                self.bytes += n_bytes
            if self._active > 0:
                self._active -= 1
            if self._active == 0 and self._busy_since is not None:
                self._busy_wall += max(0.0, now - self._busy_since)
                self._busy_since = None

    def snapshot(self, now: float | None = None) -> tuple[int, float]:
        """(кумулятивні байти, кумулятивні busy-секунди) дотепер."""
        with self._lock:
            busy = self._busy_wall
            if self._active > 0 and self._busy_since is not None and now is not None:
                busy += max(0.0, now - self._busy_since)
            return self.bytes, busy


class AdaptiveConcurrency:
    """Feedback-губернатор: скільки хеш-потоків можуть ЧИТАТИ одночасно.

    Ручна кількість потоків форсує одну статичну відповідь на два
    протилежні навантаження: ОДИН великий файл насичує USB/SATA-канал
    одним читачем (зайві читачі лише додають чергу на слабкому мосту), а
    тисячі малих файлів потребують кількох читачів, щоб сховати
    per-file-затримку. Замість вгадування губернатор ВИМІРЮЄ пропускну
    здатність (дельти байтів зі спільного DiskActivity) і hill-climb'ить
    межу конкурентності:

    * у сталому стані пробує +1 читача й дивиться наступний тік;
    * проба, що підняла згладжену пропускну здатність на >= ``_IMPROVE``,
      лишається — підйом триває;
    * проба, що регресувала — чи просто вийшла на плато — відкочується:
      та сама MB/s з меншою кількістю читачів безпечніша для крихкого
      моста;
    * марні проби відкочуються експоненційно (насичений канал не
      штрикають щотіку), межа — ``_MAX_COOLDOWN``.

    Сам пул воркерів створюється з ``cap`` потоків; лише цей гейт
    (:meth:`acquire`/:meth:`release` навколо ФІЗИЧНОГО читання кожного
    файла) вирішує, скільки з них можуть одночасно робити I/O — межа може
    рухатись у рантаймі без перебудови пулу.
    """

    _TICK_SECONDS = 2.0
    _MIN_ACTIVE_BYTES = 1 << 20  # <1 МіБ за тік = фаза обходу/пауза: без сигналу
    _IMPROVE = 1.05
    _BASE_COOLDOWN = 2
    _MAX_COOLDOWN = 32
    _WAIT_SLICE = 0.2

    def __init__(
        self,
        cap: int,
        activity: DiskActivity,
        *,
        start: int = 2,
        improve: float = _IMPROVE,
        revert_below: float | None = None,
        base_cooldown: int = _BASE_COOLDOWN,
        max_cooldown: int = _MAX_COOLDOWN,
    ) -> None:
        self.cap = max(1, int(cap))
        self.activity = activity
        self._improve = float(improve)
        self._revert_below = float(revert_below) if revert_below is not None else None
        self._base_cooldown = max(1, int(base_cooldown))
        self._max_cooldown = max(self._base_cooldown, int(max_cooldown))
        self._cond = Condition()
        self._limit = min(self.cap, max(1, int(start)))
        self._active = 0
        self._smooth: float | None = None
        self._baseline: float | None = None
        self._pending = 0
        self._cooldown = 0
        self._fail_streak = 0

    @property
    def limit(self) -> int:
        return self._limit

    # ---- бік воркер-потоку --------------------------------------------
    def acquire(self, should_stop=None) -> None:
        """Заблокуватись, доки не звільниться слот читача (або спрацює
        *should_stop*).

        На зупинку викликача пропускає БЕЗ очікування — код хешування, до
        якого він переходить, сам перевіряє cancel і повертається негайно,
        що швидше, ніж тримати N потоків заручниками цього гейту.
        """
        with self._cond:
            while self._active >= self._limit:
                if should_stop is not None and should_stop():
                    break
                self._cond.wait(self._WAIT_SLICE)
            self._active += 1

    def release(self) -> None:
        with self._cond:
            if self._active > 0:
                self._active -= 1
            self._cond.notify_all()

    def _set_limit(self, value: int) -> None:
        with self._cond:
            self._limit = max(1, min(self.cap, value))
            self._cond.notify_all()

    # ---- бік контролера -------------------------------------------------
    def control_step(self, throughput_bps: float | None) -> None:
        """Один такт рішення; ``None`` — немає сигналу (простій/обхід/пауза)."""
        if throughput_bps is None:
            return
        self._smooth = (
            throughput_bps if self._smooth is None else (self._smooth + throughput_bps) / 2.0
        )
        thr = self._smooth
        thr_mb = thr / 1e6
        if self._pending:
            base = self._baseline
            if base is not None and thr >= base * self._improve:
                self._fail_streak = 0
                self._cooldown = 0
                logger.info(
                    "Adaptive workers: probe kept -> limit %d (%.1f MB/s)",
                    self._limit, thr_mb,
                )
            elif (
                base is not None
                and self._revert_below is not None
                and thr >= base * self._revert_below
            ):
                self._cooldown = self._base_cooldown
                logger.info(
                    "Adaptive workers: plateau, limit kept at %d (%.1f MB/s)",
                    self._limit, thr_mb,
                )
            else:
                self._set_limit(self._limit - self._pending)
                self._fail_streak += 1
                self._cooldown = min(
                    self._base_cooldown * (2**self._fail_streak), self._max_cooldown
                )
                logger.info(
                    "Adaptive workers: probe reverted -> limit %d "
                    "(%.1f MB/s, cooldown %d ticks)",
                    self._limit, thr_mb, self._cooldown,
                )
            self._pending = 0
            self._baseline = None
            return
        if self._cooldown > 0:
            self._cooldown -= 1
            return
        if self._limit < self.cap:
            self._baseline = thr
            self._pending = 1
            self._set_limit(self._limit + 1)
            logger.info("Adaptive workers: probing %d readers (%.1f MB/s)", self._limit, thr_mb)

    def run(self, cancel_event: Event) -> None:
        """Семплити :attr:`activity` і крокувати контролер до скасування.

        Працює на власному daemon-потоці впродовж скану, дзеркалить
        :meth:`TemperatureGovernor.run`.
        """
        last_bytes, _busy = self.activity.snapshot()
        last_t = time.monotonic()
        while not cancel_event.wait(self._TICK_SECONDS):
            now = time.monotonic()
            b, _busy = self.activity.snapshot(now)
            db, dt = b - last_bytes, now - last_t
            last_bytes, last_t = b, now
            if dt <= 0 or db < self._MIN_ACTIVE_BYTES:
                self.control_step(None)
                continue
            self.control_step(db / dt)


# --------------------------------------------------------------------------- #
# Best-effort температура диска (smartctl, зовнішній інструмент — не приватний
# API; НЕ бандлиться, нових runtime-залежностей не додає)
# --------------------------------------------------------------------------- #
# smartctl часто стоїть ПОЗА мінімальним PATH, який успадковує GUI-застосунок:
# .app, запущений із Finder/launchd, не читає shell-профіль, тож теки Homebrew
# (/opt/homebrew/bin на Apple Silicon, /usr/local/bin на Intel) і MacPorts
# (/opt/local/bin) відсутні — shutil.which тоді хибно каже "не встановлено".
_SMARTCTL_EXTRA_DIRS = (
    "/opt/homebrew/bin",  # Homebrew — Apple Silicon
    "/usr/local/bin",  # Homebrew — Intel macOS
    "/opt/local/bin",  # MacPorts
    "/usr/sbin",
    "/sbin",
)


def _smartctl_path() -> str | None:
    """Шлях до ``smartctl`` через PATH, тоді відомі теки встановлення.
    ``None``, якщо не встановлено."""
    exe = shutil.which("smartctl")
    if exe:
        return exe
    for d in _SMARTCTL_EXTRA_DIRS:
        cand = os.path.join(d, "smartctl")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def smart_available() -> bool:
    """True, якщо бінарник ``smartctl`` знайдено (PATH чи відома тека)."""
    return _smartctl_path() is not None


def _whole_disk(dev: str) -> str | None:
    """Вузол цілого диска під партицією/томом, або ``None``.

    SMART живе на фізичному диску, не на партиції чи APFS-зрізі, тож
    ``/dev/disk3s1s1`` мусить пробуватись як ``/dev/disk3``.
    """
    m = re.match(r"(/dev/disk\d+)", dev)
    if m:
        return m.group(1) if m.group(1) != dev else None
    return None


def _device_for_path(path: os.PathLike | str) -> str | None:
    """Best-effort: вузол фізичного пристрою під *path* (для smartctl)."""
    p = os.fspath(path)
    try:
        out = subprocess.run(
            ["df", p], capture_output=True, text=True, timeout=5
        ).stdout.splitlines()
        if len(out) >= 2:
            dev = out[1].split()[0]
            if dev.startswith("/dev/"):
                return dev
    except Exception:  # noqa: BLE001
        return None
    return None


def _candidate_devices(path: os.PathLike | str) -> list[str]:
    """Вузол(-ли) пристрою для проби *path*: джерело монтування і, якщо
    воно партиція/APFS-зріз, вузол цілого диска (там і живе SMART)."""
    dev = _device_for_path(path)
    if not dev:
        return []
    candidates = [dev]
    whole = _whole_disk(dev)
    if whole and whole not in candidates:
        candidates.append(whole)
    return candidates


def _parse_temp_c(text: str) -> float | None:
    """Витягти температуру диска в Цельсіях з виводу ``smartctl -A``/``-x``.

    Для рядка SMART-атрибута читається лише RAW_VALUE (10-та колонка) і
    лише її провідне ціле — НІКОЛИ нормалізоване значення 0–253
    ("здоров'я"), яке для температурного атрибута виглядає як фальшиві
    ~100 °C. Температура самого диска має пріоритет над airflow/case-
    сенсором.
    """
    drive_temp: float | None = None
    other_temp: float | None = None

    def _consider(val: float, is_airflow: bool) -> None:
        nonlocal drive_temp, other_temp
        if is_airflow:
            if other_temp is None:
                other_temp = val
        elif drive_temp is None:
            drive_temp = val

    for line in text.splitlines():
        low = line.lower()
        if "temperature" not in low and "airflow" not in low:
            continue
        # Пропустити рядки SETPOINT (теж містять "Temperature", але це межі,
        # не жива температура): NVMe "Warning/Critical Comp. Temp. Threshold",
        # SCSI/SAS "Drive Trip Temperature". Просочена межа 65-85 °C
        # призвела б до вічної паузи на холодному диску.
        if "threshold" in low or "trip" in low:
            continue
        toks = line.split()
        is_airflow = "airflow" in low or "case" in low
        if toks and toks[0].isdigit() and len(toks) >= 10:
            m = re.match(r"\d+", toks[9])
            if m:
                v = int(m.group())
                if 0 < v < 120:
                    _consider(float(v), is_airflow)
            continue
        candidate: int | None = None
        for i, tok in enumerate(toks):
            s = tok.strip(":,").rstrip("Cc°")
            if not s.isdigit():
                continue
            v = int(s)
            if not (0 < v < 120):
                continue
            nxt = toks[i + 1].lower() if i + 1 < len(toks) else ""
            has_unit = (
                nxt.startswith("c") or "celsius" in nxt
                or tok.rstrip(":,").lower().endswith("c")
            )
            if has_unit:
                candidate = v
                break
            if candidate is None:
                candidate = v
        if candidate is not None:
            _consider(float(candidate), is_airflow)
    return drive_temp if drive_temp is not None else other_temp


def _smartctl_temp(device: str, should_stop=None) -> float | None:
    """Прочитати температуру *device* через smartctl, перебираючи типи.

    Кешує тип пристрою, що спрацював, тож повторні проби дешеві.
    """
    cached = _DEVICE_DTYPE_CACHE.get(device, "<unset>")
    dtypes: tuple[str | None, ...]
    if cached != "<unset>":
        dtypes = (cached, *(d for d in _DEVICE_TYPES if d != cached))  # type: ignore[misc]
    else:
        dtypes = _DEVICE_TYPES
    exe = _smartctl_path()
    if exe is None:
        return None
    for dtype in dtypes:
        if should_stop is not None and should_stop():
            return None
        cmd = [exe, "-A"]
        if dtype:
            cmd += ["-d", dtype]
        cmd.append(device)
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=_SMARTCTL_TIMEOUT
            ).stdout
        except Exception:  # noqa: BLE001
            continue
        temp = _parse_temp_c(out)
        if temp is not None:
            _DEVICE_DTYPE_CACHE[device] = dtype
            return temp
    return None


def read_disk_temperature(path: os.PathLike | str, should_stop=None) -> float | None:
    """Поточна температура диска в °C, або ``None``, якщо непрочитна.

    ``None`` — звичайний випадок для USB-корпусів без проходу SMART, або
    коли ``smartctl`` не встановлено — виклики мусять трактувати це як
    "режим температури недоступний", не як помилку.
    """
    if not smart_available():
        return None
    for device in _candidate_devices(path):
        if should_stop is not None and should_stop():
            return None
        temp = _smartctl_temp(device, should_stop=should_stop)
        if temp is not None:
            return temp
    return None


class TemperatureGovernor:
    """Фоновий семплер, що ставить скан на паузу для охолодження диска.

    Керує ТИМ САМИМ ``pause_event``, що й ручна кнопка Паузи: коли диск
    занадто гарячий — подія встановлюється (хешування блокується → диск
    простоює й холоне); коли температура падає нижче ``cap - hysteresis``
    — подія знімається. РУЧНА пауза ніколи не перезаписується — рішення
    лишається за викликачем.

    Декілька коренів можуть лежати на різних фізичних дисках; губернатор
    семплить кожен окремий пристрій і керує за НАЙГАРЯЧІШИМ.
    """

    def __init__(self, paths, cap_c: float) -> None:
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        self.paths: list[str] = []
        seen_devices: set[str] = set()
        for p in paths:
            p = os.fspath(p)
            devs = _candidate_devices(p)
            key = devs[-1] if devs else p
            if key in seen_devices:
                continue
            seen_devices.add(key)
            self.paths.append(p)
        self.cap_c = float(cap_c)
        self.last_temp: float | None = None
        self.too_hot = False

    def sample(self, should_stop=None) -> float | None:
        """Прочитати найгарячіший пристрій, оновити :attr:`too_hot`.

        ``None`` лише коли НІЯКИЙ пристрій непрочитний.
        """
        temps = [
            t
            for t in (read_disk_temperature(p, should_stop=should_stop) for p in self.paths)
            if t is not None
        ]
        if not temps:
            self.last_temp = None
            return None
        t = max(temps)
        self.last_temp = t
        if t >= self.cap_c:
            self.too_hot = True
        elif t <= self.cap_c - _HYSTERESIS:
            self.too_hot = False
        return t

    def run(self, on_change, cancel_event: Event) -> None:
        """Опитувати до *cancel_event*; кликати ``on_change(too_hot, temp)``
        при зміні. Працює на власному daemon-потоці впродовж скану.

        Читання, що працювало й раптом стало непрочитним (нестабільний
        USB-міст), НЕ знімає паузу-охолодження миттєво; лише після кількох
        поспіль пропусків знімає паузу й повідомляє недоступність — але
        продовжує опитувати повільніше (зайнятий USB-SATA міст може не
        відповідати 15 с під важким I/O без того, щоб диск справді зник).
        """
        last: bool | None = None
        ever_read = False
        misses = 0
        degraded = False
        while not cancel_event.is_set():
            t = self.sample(should_stop=cancel_event.is_set)
            if cancel_event.is_set():
                return
            if t is None:
                if not ever_read or misses >= _MAX_TRANSIENT_MISSES:
                    if not degraded:
                        on_change(False, None)
                        degraded = True
                        last = None
                    cancel_event.wait(_POLL_SECONDS * 6)
                    continue
                misses += 1
                cancel_event.wait(_POLL_SECONDS)
                continue
            if degraded:
                logger.info("Disk temperature readable again — governance resumed")
                degraded = False
            misses = 0
            ever_read = True
            if self.too_hot != last:
                logger.info(
                    "Disk %.0f°C (cap %.0f°C) -> %s",
                    t, self.cap_c, "pause to cool" if self.too_hot else "resume",
                )
                on_change(self.too_hot, t)
                last = self.too_hot
            cancel_event.wait(_POLL_SECONDS)
