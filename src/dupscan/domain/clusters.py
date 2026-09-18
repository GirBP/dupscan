"""Транзитивні кластери тек, зв'язані спільним дубльованим вмістом.

Адаптовано з DupFinder src/dupfinder/duplicate_clusters.py — ЛИШЕ чиста
функція `build_duplicate_clusters`
(union-find над готовими даними); `find_duplicate_clusters` (окремий
walk+hash, дублює core.scan) НЕ портовано — ТЗ явно вимагає "БЕЗ нового
I/O — тільки з готового ScanResult".

На відміну від DirGroup (Merkle-доказ ЦІЛОГО піддерева — core.py) кластер
відповідає на інше питання: «які теки, разом узяті, заплутані копіями
тих самих файлів?». Дві теки лінкуються, коли поділяють хоча б один
дубльований файл; зв'язок транзитивний (A-B одним файлом, B-C іншим —
усі три в одному кластері) через union-find над ключами директорій.

DupScan уже має за побудовою те, що DupFinder збирав окремим проходом:
`ScanResult.class_paths` — готова карта клас→шляхи, тож Pass 1 джерела
(бакетування file_hash по digest) тут зайве — просто читаємо class_paths.

Read-only: жодна деструктивна дія не націлюється на кластер напряму
(структурний інваріант, перевірений тестами).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dupscan.domain.core import ScanResult


@dataclass
class DirCluster:
    """Зв'язна компонента тек, поєднаних спільними дубльованими файлами.

    ``dirs`` — теки-члени (НЕ "files", на відміну від джерела: там ця
    назва — спадок GUI-моделі, що очікує files-подібний об'єкт; DupScan
    не потребує цієї сумісності, тож назва прямо відповідає змісту).
    """

    dirs: list[str]
    size: int  # сумарні байти КОЖНОЇ дубльованої копії в кластері
    dup_file_count: int  # дубльованих файлів-примірників у всьому кластері
    class_count: int  # скільки РІЗНИХ класів-дублікатів зв'язують кластер
    per_dir_total: dict[str, int]  # тека -> усі файли безпосередньо в ній
    per_dir_dup: dict[str, int]  # тека -> з них, скільки дубльовані

    @property
    def count(self) -> int:
        return len(self.dirs)

    def percent_for(self, path: str) -> float:
        """Частка файлів *path*, що є членами дубль-груп.

        0.0 і коли у *path* взагалі немає файлів (захист від ділення на
        нуль), і коли *path* не є членом цього кластера.
        """
        total = self.per_dir_total.get(path, 0)
        if total == 0:
            return 0.0
        return self.per_dir_dup.get(path, 0) / total * 100


@dataclass
class ClusterResult:
    clusters: list[DirCluster] = field(default_factory=list)

    @property
    def total_clusters(self) -> int:
        return len(self.clusters)

    @property
    def total_duplicate_bytes(self) -> int:
        return sum(c.size for c in self.clusters)


class _UnionFind:
    """Мінімальний union-find (стиснення шляху, без union-за-рангом —
    компоненти тут завжди дрібні, ранг не грає ролі) над рядковими ключами."""

    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        root = x
        while self._parent.get(root, root) != root:
            root = self._parent[root]
        while self._parent.get(x, x) != root:
            self._parent[x], x = root, self._parent.get(x, x)
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def build_duplicate_clusters(
    res: "ScanResult", *, min_files: int = 1,
) -> list[DirCluster]:
    """Згорнути готовий ScanResult у кластери тек. Чисто — жодного I/O,
    споживає лише res.dir_files/file_class/class_paths/file_meta, як вони
    вже стоять після скану чи завантаження історичної сесії.

    Клас — «дубльований» лише коли має >=2 члени (за побудовою
    file_class/class_paths: унікальний файл отримує ВЛАСНИЙ, ніким не
    поділюваний клас, core.scan.uniq) І ці члени лежать щонайменше у 2
    РІЗНИХ теках (копії в ОДНІЙ теці не додають міжтечового лінка).
    """
    # ---- Крок 1: клас -> різні теки, де лежить копія ----------------------
    class_dirs: dict[str, set[str]] = {}
    for cls, paths in res.class_paths.items():
        if len(paths) < 2:
            continue
        dirs = {os.path.dirname(p) for p in paths}
        if len(dirs) < 2:  # усі копії в ОДНІЙ теці -> міжтечового лінка нема
            continue
        class_dirs[cls] = dirs

    # ---- Крок 2: об'єднати теки, що співіснують у класі з >=2 теками ------
    uf = _UnionFind()
    for dirs in class_dirs.values():
        it = iter(dirs)
        first = next(it)
        for d in it:
            uf.union(first, d)

    # ---- Крок 3: per-тека total/dup лише для тек, що хоч раз лінкувались --
    linked = {d for dirs in class_dirs.values() for d in dirs}
    per_dir_dup: dict[str, int] = {}
    per_dir_dup_bytes: dict[str, int] = {}
    for d in linked:
        dup_here = 0
        dup_bytes = 0
        for fp in res.dir_files.get(d, []):
            # Окрема назва від "cls" у Кроці 1 вище (той самий іменний
            # конфлікт, що docs у Блоці M) — тут str | None, там str.
            dup_cls = res.file_class.get(fp)
            if dup_cls is None or len(res.class_paths.get(dup_cls, ())) < 2:
                continue
            dup_here += 1
            meta = res.file_meta.get(fp)
            if meta is not None:
                dup_bytes += meta.size
        per_dir_dup[d] = dup_here
        per_dir_dup_bytes[d] = dup_bytes

    # ---- Крок 4: зібрати компоненти (лише теки, що колись об'єднувались) --
    components: dict[str, list[str]] = {}
    for d in linked:
        components.setdefault(uf.find(d), []).append(d)

    # Скільки РІЗНИХ класів-дублікатів зв'язують кожен корінь компоненти:
    # усі теки одного класу за побудовою в тому самому корені (unioned разом
    # у Кроці 2), тож досить одного представника класу.
    class_count_by_root: dict[str, int] = {}
    for dirs in class_dirs.values():
        root = uf.find(next(iter(dirs)))
        class_count_by_root[root] = class_count_by_root.get(root, 0) + 1

    clusters: list[DirCluster] = []
    for root, members in components.items():
        if len(members) < 2:  # виродження union-find; не мало б статись, захист
            continue
        dup_file_count = sum(per_dir_dup[d] for d in members)
        if dup_file_count < min_files:
            continue
        size = sum(per_dir_dup_bytes[d] for d in members)
        clusters.append(
            DirCluster(
                dirs=sorted(members),
                size=size,
                dup_file_count=dup_file_count,
                class_count=class_count_by_root.get(root, 0),
                per_dir_total={d: len(res.dir_files.get(d, [])) for d in members},
                per_dir_dup={d: per_dir_dup[d] for d in members},
            )
        )
    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters
