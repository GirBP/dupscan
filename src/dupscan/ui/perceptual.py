"""Перцептивна подібність зображень — «Схожі фото (підказка)».

Адаптовано з DupFinder src/dupfinder/imagehash.py +
src/dupfinder/perceptual_scanner.py.

**НАЙВАЖЛИВІШЕ (не послаблювати ніколи):** перцептивна група — ПІДКАЗКА,
не доказ. Вона не має точного контентного дайджеста (BLAKE3), лише оцінку
візуальної схожості — стиснутий/переконвертований/зменшений дублікат дає
той самий геш, але так само дає й ІНША картинка з подібною композицією.
Тому PerceptualGroup — структурно ОКРЕМИЙ тип, не FileGroup: жодного поля
на кшталт digest/клас, і деструктивні шляхи (app.py: delete_checked,
_delete_duplicate_paths, _trash_current_duplicate,
_verify_snapshot_group_then_trash) явно відмовляють цьому типу — див.
tests/test_perceptual_readonly_invariant.py, по тесту на кожну точку
входу.

**Залежності:** джерело хешує через Pillow (`PIL.Image`). DupScan не
додає Pillow — PySide6 (уже runtime-залежність) має QImage, придатний
для того самого dHash без жодної нової залежності. QImage, на відміну
від QPixmap, документовано безпечний поза GUI-потоком — САМЕ тому Qt
рекомендує його для фонового декодування, що й потрібно тут (воркер, не
GUI-потік).

**Обхід:** повторює правила core.scan (EXCLUDE_NAMES/EXCLUDE_PREFIXES,
bundle-теки не розкриваються, AppleDouble-супутники невидимі,
symlink-теки/файли не читаються, dataless/iCloud НІКОЛИ не читається) —
але БЕЗ дублікат-бухгалтерії core.scan, лише збирає шляхи зображень.
"""

from __future__ import annotations

import logging
import os
import stat as stat_mod
import threading
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

import dupscan.domain.core as core

logger = logging.getLogger(__name__)

HASH_SIZE = 8  # сітка різниць 8x8 -> 64-бітний геш
DEFAULT_MAX_DISTANCE = 10  # бітів Геммінга; ≤10/64 ≈ «виглядає так само»

IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
)


def is_image(path: str) -> bool:
    """Дешева перевірка за розширенням — фільтр ДО декодування."""
    return os.path.splitext(path)[1].lower() in IMAGE_SUFFIXES


@dataclass
class PerceptualGroup:
    """Кластер зображень, що виглядають однаково. НЕ підтверджений дублікат.

    Навмисно НЕ FileGroup: немає digest/checked/жодного поля, яке
    деструктивний код міг би прийняти за доказ. `files` — шляхи-члени.
    """

    files: list[str]

    @property
    def count(self) -> int:
        return len(self.files)


@dataclass
class PerceptualResult:
    groups: list[PerceptualGroup] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    images_hashed: int = 0

    @property
    def total_groups(self) -> int:
        return len(self.groups)

    @property
    def total_images(self) -> int:
        return sum(g.count for g in self.groups)


def dhash_from_gray(pixels: list[int], width: int, height: int) -> int:
    """Рядковий difference-hash з плаского grayscale-буфера width*height.

    Буфер — height рядків по width пікселів, де width на один ширший за
    сторону геша, тож кожен рядок дає width-1 біт порівняння. Чисто —
    без QImage, тож алгоритм тестується ізольовано.
    """
    bits = 0
    for row in range(height):
        base = row * width
        for col in range(width - 1):
            left = pixels[base + col]
            right = pixels[base + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def _dihedral_variants(grid: list[list[int]]) -> list[list[list[int]]]:
    """8 варіантів квадратної сітки: 4 обертання × дзеркало."""
    variants = []
    current = grid
    for _ in range(4):
        variants.append(current)
        variants.append([row[::-1] for row in current])  # дзеркало по X
        # поворот на 90°: транспонувати і розвернути рядки
        current = [list(row) for row in zip(*current[::-1])]
    return variants


def perceptual_hash(path: str, hash_size: int = HASH_SIZE) -> int | None:
    """Канонічний dHash зображення, або None, якщо не вдалось декодувати.

    v2 (2.23.0): хеш інваріантний до обертань на
    90/180/270 і дзеркала — обернена копія фото групується з оригіналом
    (паритет із czkawka). Механіка: сітка читається ОДИН раз квадратом
    (side×side), далі 8 діедральних варіантів рахуються чистим Python на
    ~81 значенні, з кожного — dHash по перших hash_size рядках, канонічний
    хеш = мінімум. Групування (union-find по Геммінгу) незмінне: йому
    байдуже, звідки взявся int.

    Ніколи не кидає: непридатний шлях, зіпсований файл чи не-зображення
    дають None — виклики просто пропускають. QImage(path) для
    нечитабельного/не-зображення дає null-образ (isNull()), не
    виключення — try/except лишається на випадок сюрпризів біндингу.
    """
    try:
        img = QImage(path)
        if img.isNull():
            return None
        side = hash_size + 1
        gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
        small = gray.scaled(
            side, side,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        if small.isNull() or small.width() != side or small.height() != side:
            return None
        grid = [
            [small.pixelColor(x, y).red() for x in range(side)]
            for y in range(side)
        ]
        return min(
            dhash_from_gray(
                [value for row in variant[:hash_size] for value in row],
                side, hash_size)
            for variant in _dihedral_variants(grid)
        )
    except Exception as exc:  # noqa: BLE001 — декодування ніколи не кидає
        logger.debug("perceptual_hash skipped %s: %s", path, exc)
        return None


def hamming(a: int, b: int) -> int:
    """Кількість бітів, що різняться між двома гешами."""
    return (a ^ b).bit_count()


def group_similar(
    hashes: dict[str, int], max_distance: int = DEFAULT_MAX_DISTANCE,
) -> list[list[str]]:
    """Кластеризувати шляхи, чиї перцептивні геші в межах max_distance бітів.

    Транзитивне single-linkage через union-find: A~B і B~C об'єднує всі
    три, навіть якщо A і C трохи далі одне від одного. Повертає лише
    групи з 2+ членів, кожна відсортована для стабільного виводу.
    O(n²) порівнянь — прийнятно для опційної підказки; обсяг обирає
    викликач.
    """
    items = list(hashes.items())
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for i in range(n):
        for j in range(i + 1, n):
            if hamming(items[i][1], items[j][1]) <= max_distance:
                union(i, j)

    clusters: dict[int, list[str]] = {}
    for idx, (path, _digest) in enumerate(items):
        clusters.setdefault(find(idx), []).append(path)

    return [sorted(group) for group in clusters.values() if len(group) >= 2]


def _walk_images(
    roots: list[str],
    *,
    cancel: threading.Event,
    pause: threading.Event | None = None,
    skip_cloud: bool = True,
    errors: list[str] | None = None,
) -> list[str]:
    """Обхід за правилами core.scan (виключення/bundle/symlink/dataless),
    але лише ЗБИРАЄ шляхи зображень — жодної дублікат-бухгалтерії."""
    found: list[str] = []
    seen: set[str] = set()
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            if errors is not None:
                errors.append(f"не тека: {root}")
            continue
        for dirpath, dirnames, filenames in os.walk(
            root, topdown=True, followlinks=False,
            onerror=lambda e: errors.append(str(e)) if errors is not None else None,
        ):
            core._wait_if_paused(pause, cancel)
            if cancel.is_set():
                return found
            keep = []
            for d in dirnames:
                full_d = os.path.join(dirpath, d)
                if core._excluded(full_d, d) or core._is_bundle(d):
                    continue  # виключення/bundle: тека НЕ розкривається
                if os.path.islink(full_d):
                    continue  # symlink-тека: не читаємо крізь неї
                keep.append(d)
            dirnames[:] = keep
            for name in filenames:
                if cancel.is_set():
                    return found
                if core.is_appledouble(name):
                    continue  # метадані сусіднього файла, не фото
                if not is_image(name):
                    continue
                fp = os.path.join(dirpath, name)
                try:
                    st = os.lstat(fp)
                except OSError as exc:
                    if errors is not None:
                        errors.append(str(exc))
                    continue
                if stat_mod.S_ISLNK(st.st_mode):
                    continue  # symlink-файл: не читаємо крізь нього
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                if skip_cloud and core._is_dataless(st):
                    # тіло в хмарі: читання = тихе викачування — НІКОЛИ.
                    if errors is not None:
                        errors.append(
                            f"iCloud-файл без локального вмісту — "
                            f"пропущено: {fp}")
                    continue
                key = os.path.normcase(fp)
                if key in seen:
                    continue
                seen.add(key)
                found.append(fp)
    return found


def find_similar_images(
    roots: list[str],
    *,
    max_distance: int = DEFAULT_MAX_DISTANCE,
    cancel: threading.Event | None = None,
    pause: threading.Event | None = None,
    progress=None,
    skip_cloud: bool = True,
) -> PerceptualResult:
    """Кластеризувати візуально схожі зображення під *roots*.

    Послідовно (не через ThreadPoolExecutor, як core.scan) — джерело
    саме так і робить (простий for-цикл), а «підказка» не мусить
    ускладнюватись паралелізмом заради опційної, некритичної для доказу
    смуги. cancel/pause шануються так само, як точний скан.
    """
    cancel = cancel or threading.Event()
    errors: list[str] = []
    images = _walk_images(
        roots, cancel=cancel, pause=pause, skip_cloud=skip_cloud, errors=errors)
    total = len(images)
    hashes: dict[str, int] = {}
    for i, path in enumerate(images):
        if cancel.is_set():
            break
        core._wait_if_paused(pause, cancel)
        if cancel.is_set():
            break
        digest = perceptual_hash(path)
        if digest is not None:
            hashes[path] = digest
        if progress is not None and ((i + 1) % 10 == 0 or i + 1 == total):
            progress("Порівнюю зображення", i + 1, total)
    groups = [PerceptualGroup(files=g) for g in group_similar(hashes, max_distance)]
    logger.info(
        "Перцептивний скан: %d зображень захешовано, %d груп(и) схожості",
        len(hashes), len(groups),
    )
    return PerceptualResult(groups=groups, errors=errors, images_hashed=len(hashes))
