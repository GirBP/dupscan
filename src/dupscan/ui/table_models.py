"""Qt-моделі результатів DupScan: групи дублікатів і пари подібності."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor

import dupscan.domain.clusters as clusters
import dupscan.ui.perceptual as perceptual
import dupscan.domain.product as product
import dupscan.ui.scale_ui as scale_ui
from dupscan.format import human, when

COLS = ("До Кошика / Шлях", "Розмір", "Створено", "Змінено")
# QTreeView синхронно перераховує геометрію всіх показаних дітей після
# layoutChanged. 500 рядків на кожну розгорнуту групу давали секундні паузи
# вже на кількох десятках груп; 100 тримає один UI-квант коротким, а наступні
# сторінки Qt запитує через fetchMore() під час прокручування.
PAGE_SIZE = 100
ROOT_PAGE_SIZE = 500


class GroupModel(QAbstractItemModel):
    """Група -> шляхи. Чекбокси на всіх рядах; єдине обмеження — не можна
    позначити ВСІ елементи групи (щонайменше один завжди лишається)."""

    sortStarted = Signal()
    sortFinished = Signal()
    checksChanged = Signal()

    def __init__(self, dates_fn):
        super().__init__()
        self.groups: list = []
        self.checked: set[str] = set()
        self.dates_fn = dates_fn  # path -> (btime_ns, mtime_ns) | None
        self.warn = lambda msg: None
        # Інжектиться застосунком (тека-еталон); модель не знає preferences.
        self.protected_checker: Callable[[str], bool] | None = None
        # Мітка статусу поверх рядка — за зразком
        # SimModel.tags: view-шар, знімок/сесію не чіпає. Ключ — сам шлях
        # (на відміну від пари, рядок групи однозначно ідентифікується ним).
        self.tags: dict[str, str] = {}
        # View-порядок відокремлений від результату скану: сортування не
        # перебудовує об'єкти груп і не запускає сотні тисяч data() через
        # QSortFilterProxyModel.
        self._group_order: list[int] = []
        self._all_group_order: list[int] = []
        self._group_pos: dict[int, int] = {}
        self._root_fetched = 0
        self._child_order: list[list[int]] = []
        self._all_child_order: list[list[int]] = []
        self._fetched: list[int] = []
        self._display: list[list[list[str | None]]] = []
        self._sort_keys: list[list[tuple[object, object, object, object]]] = []
        self._categories: list[list[str]] = []
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder
        self._filter_text = ""
        self._category_filter = "all"
        self._view_preset = "all"
        self._data_generation = 0
        self._filter_serial = 0
        # Стабільні id реально розгорнутих груп. Їх оновлює QTreeView через
        # set_expanded(); сортування більше не обходить усі кореневі рядки.
        self._expanded: set[int] = set()
        self.keeper_fn = None

    @staticmethod
    def prepare_groups(groups: list, dates_fn, cancel=None):
        """Build immutable display/sort payload off the GUI thread."""
        display = []
        sort_keys = []
        all_categories = []
        child_order = []
        valid_paths: set[str] = set()
        processed = 0
        for group_index, g in enumerate(groups):
            shown, keys, categories = [], [], []
            size_text = human(g.size)
            for p in g.paths:
                if cancel is not None and processed % 4096 == 0 and cancel.is_set():
                    return None
                dates = dates_fn(p)
                born = dates[0] if dates else 0
                changed = dates[1] if dates else 0
                # Дати форматуються ліниво лише для видимих комірок. Для
                # сортування вже є дешеві цілі числа born/changed.
                shown.append([p, size_text, None if dates else "—",
                              None if dates else "—"])
                keys.append((p.casefold(), g.size, born, changed))
                categories.append(product.category_for_path(p))
                valid_paths.add(p)
                processed += 1
            display.append(shown)
            sort_keys.append(keys)
            all_categories.append(categories)
            child_order.append(list(range(len(g.paths))))
            if cancel is not None and group_index % 1024 == 0 and cancel.is_set():
                return None
        return (
            display, sort_keys, all_categories,
            child_order, valid_paths,
        )

    def apply_prepared_groups(self, groups: list, payload) -> None:
        self._data_generation += 1
        self.beginResetModel()
        display, sort_keys, categories, child_order, valid_paths = payload
        self.groups = groups
        self.checked.intersection_update(valid_paths)
        self._display = display
        self._sort_keys = sort_keys
        self._categories = categories
        self._child_order = []
        self._all_child_order = child_order
        self._all_group_order = list(range(len(groups)))
        self._apply_sort_order()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._group_order))
        self._fetched = [min(PAGE_SIZE, len(order)) for order in self._child_order]
        self.endResetModel()
        self.checksChanged.emit()

    def set_groups(self, groups: list) -> None:
        payload = self.prepare_groups(groups, self.dates_fn)
        self.apply_prepared_groups(groups, payload)

    def _apply_sort_order(self) -> None:
        if self._view_preset == "largest":
            self._all_group_order.sort(
                key=lambda gi: self.groups[gi].wasted, reverse=True)
        if self._sort_column >= 0:
            reverse = self._sort_order == Qt.DescendingOrder
            # Групи вже приходять у корисному порядку (найбільша економія
            # першою). Заголовки «Шлях/Дата/Розмір» описують їхні дочірні
            # копії, тому не переставляємо корені й не змушуємо QTreeView
            # перебудовувати всі розгорнуті піддерева.
            for gi, order in enumerate(self._all_child_order):
                keys = self._sort_keys[gi]
                # _sort_keys навмисно tuple[object, object,
                # object, object] (рядок 75) — кожна колонка порівнюється
                # своїм типом (str/int/дата), спільного вужчого типу немає.
                order.sort(
                    key=lambda ri: keys[ri][self._sort_column],  # type: ignore[arg-type, return-value]
                    reverse=reverse)
        self._apply_filter()

    def _apply_filter(self) -> None:
        query = self._filter_text
        category = self._category_filter
        preset = self._view_preset

        def group_matches(gi: int) -> bool:
            paths = self.groups[gi].paths
            on_volume = any(path.startswith("/Volumes/") for path in paths)
            missing_metadata = any(
                self._display[gi][ri][2] == "—"
                for ri in range(len(self._display[gi]))
            )
            if preset in {"all", "largest"}:
                return True
            if preset == "safe":
                return len(paths) > 1 and not missing_metadata
            if preset == "review":
                return missing_metadata or on_volume
            if preset == "network":
                # macOS mounts removable and network volumes below /Volumes.
                # The UI names this honestly as a combined source view.
                return on_volume
            return True

        if query or category != "all" or preset not in {"all", "largest"}:
            self._child_order = [
                [ri for ri in order
                 # Той самий object-tuple _sort_keys, що вище
                 # — колонка [0] завжди str за побудовою (ім'я для фільтра).
                 if (not query
                     or query in self._sort_keys[gi][ri][0])  # type: ignore[operator]
                 and (category == "all" or self._categories[gi][ri] == category)]
                for gi, order in enumerate(self._all_child_order)
            ]
            self._group_order = [gi for gi in self._all_group_order
                                 if self._child_order[gi] and group_matches(gi)]
        else:
            self._child_order = [list(order) for order in self._all_child_order]
            self._group_order = list(self._all_group_order)
        self._group_pos = {gi: row for row, gi in enumerate(self._group_order)}

    def set_filter(self, text: str) -> None:
        query = text.strip().casefold()
        if query == self._filter_text:
            return
        self._filter_serial += 1
        self.beginResetModel()
        self._filter_text = query
        self._apply_filter()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._group_order))
        self._fetched = [min(PAGE_SIZE, len(order)) for order in self._child_order]
        self.endResetModel()

    def set_category(self, category: str) -> None:
        category = category if category in product.CATEGORY_LABELS else "all"
        if category == self._category_filter:
            return
        self._filter_serial += 1
        self.beginResetModel()
        self._category_filter = category
        self._apply_filter()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._group_order))
        self._fetched = [min(PAGE_SIZE, len(order)) for order in self._child_order]
        self.endResetModel()

    def set_view_preset(self, preset: str) -> None:
        """Apply a validated in-memory view; never performs filesystem I/O."""
        if preset not in scale_ui.PRESETS:
            preset = "all"
        if preset == self._view_preset:
            return
        self._filter_serial += 1
        self.beginResetModel()
        self._view_preset = preset
        self._all_group_order = list(range(len(self.groups)))
        self._apply_sort_order()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._group_order))
        self._fetched = [min(PAGE_SIZE, len(order)) for order in self._child_order]
        self.endResetModel()

    def prepare_filter(self, text: str, cancel: threading.Event):
        """Зняти immutable snapshot і повернути роботу без доступу до Qt.

        Snapshot порядків захищає worker від одночасного сортування/reset.
        Серійний номер і generation не дозволяють старому пошуку застосуватися
        до новіших даних або нового тексту.
        """
        query = text.strip().casefold()
        category = self._category_filter
        preset = self._view_preset
        self._filter_serial += 1
        serial = self._filter_serial
        generation = self._data_generation
        orders = tuple(tuple(order) for order in self._all_child_order)
        keys = self._sort_keys
        categories = self._categories
        group_order = tuple(self._all_group_order)
        group_allowed = tuple(
            True if preset in {"all", "largest"}
            else (
                len(self.groups[gi].paths) > 1
                and not any(row[2] == "—" for row in self._display[gi])
            ) if preset == "safe"
            else (
                any(row[2] == "—" for row in self._display[gi])
                or any(path.startswith("/Volumes/")
                       for path in self.groups[gi].paths)
            ) if preset == "review"
            else any(path.startswith("/Volumes/")
                     for path in self.groups[gi].paths)
            for gi in range(len(self.groups))
        )

        def compute():
            if not query and category == "all":
                children = [list(order) for order in orders]
            else:
                children = []
                for gi, order in enumerate(orders):
                    matched: list[int] = []
                    for start in range(0, len(order), 4096):
                        if cancel.is_set():
                            return None
                        matched.extend(
                            ri for ri in order[start:start + 4096]
                            # Той самий object-tuple _sort_keys
                            # інваріант, що в _apply_filter вище.
                            if (not query
                                or query in keys[gi][ri][0])  # type: ignore[operator]
                            and (category == "all"
                                 or categories[gi][ri] == category)
                        )
                    children.append(matched)
            if cancel.is_set():
                return None
            visible = [
                gi for gi in group_order if children[gi] and group_allowed[gi]
            ]
            return (
                generation, serial, query, category, preset, children, visible)

        return serial, compute

    def apply_filter_result(self, payload) -> str:
        """Застосувати готовий порядок у GUI; повернути applied/stale/old."""
        generation, serial, query, category, preset, children, visible = payload
        if serial != self._filter_serial:
            return "old"
        if generation != self._data_generation:
            return "stale"
        if preset != self._view_preset:
            return "old"
        self.beginResetModel()
        self._filter_text = query
        self._category_filter = category
        self._child_order = children
        self._group_order = visible
        self._group_pos = {gi: row for row, gi in enumerate(visible)}
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._group_order))
        self._fetched = [min(PAGE_SIZE, len(order)) for order in children]
        self.endResetModel()
        return "applied"

    def sort(self, column, order=Qt.AscendingOrder):
        if not 0 <= column < len(COLS):
            return
        self.sortStarted.emit()
        self._data_generation += 1
        self._sort_column = column
        self._sort_order = order
        self._apply_sort_order()
        # Порядок коренів, кількість і геометрія рядків не змінилися. View
        # перечитає лише видимі комірки через viewport.update() у
        # _restore_sort_state; глобальні layout/dataChanged тут і створювали
        # затримку, пропорційну всім розгорнутим рядкам.
        self.sortFinished.emit()

    def set_expanded(self, idx: QModelIndex, on: bool) -> None:
        if not idx.isValid() or idx.internalId() != 0:
            return
        gi = self.root_id(idx.row())
        if gi is None:
            return
        if on:
            self._expanded.add(gi)
        else:
            self._expanded.discard(gi)

    def root_id(self, row: int) -> int | None:
        return (
            self._group_order[row]
            if 0 <= row < self._root_fetched else None
        )

    def root_row(self, item_id: int) -> int | None:
        row = self._group_pos.get(item_id)
        return row if row is not None and row < self._root_fetched else None

    def _ensure_root_visible(self, item_id: int) -> int | None:
        row = self._group_pos.get(item_id)
        if row is None:
            return None
        while row >= self._root_fetched:
            self._load_more_roots()
        return row

    def total_children(self) -> int:
        return sum(len(order) for order in self._child_order)

    def item_key(self, idx):
        if not idx.isValid():
            return None
        if idx.internalId() == 0:
            gi = self.root_id(idx.row())
            return ("g", gi, idx.column()) if gi is not None else None
        gi = int(idx.internalId()) - 1
        if (not 0 <= gi < len(self._child_order)
                or idx.row() >= self._fetched[gi]
                or not 0 <= idx.row() < len(self._child_order[gi])):
            return None
        return ("c", gi, self._child_order[gi][idx.row()], idx.column())

    def index_for_key(self, key):
        if not key:
            return QModelIndex()
        if key[0] == "g":
            row = self._ensure_root_visible(key[1])
            return self.index(row, key[2]) if row is not None else QModelIndex()
        if key[0] == "c" and 0 <= key[1] < len(self._child_order):
            try:
                row = self._child_order[key[1]].index(key[2])
            except ValueError:
                return QModelIndex()
            parent_row = self._ensure_root_visible(key[1])
            if parent_row is not None:
                return self.index(row, key[3], self.index(parent_row, 0))
        return QModelIndex()

    def index(self, row, col, parent=QModelIndex()):
        if not parent.isValid():
            return self.createIndex(row, col, 0)
        if parent.internalId() != 0:
            return QModelIndex()
        # Один виклик root_id замість двох: якщо перевірку "is None" і
        # використання рознести на два виклики, mypy бачить другий як
        # незалежний int | None, хоч метод детермінований.
        gi = self.root_id(parent.row())
        if gi is None:
            return QModelIndex()
        return self.createIndex(row, col, gi + 1)

    def parent(self, idx=None):
        # QAbstractItemModel.parent є перевантаженим — без
        # аргументу це QObject.parent() (об'єктне дерево Qt), з аргументом —
        # ця, дерево-навігаційна версія. Стара сигнатура (idx обов'язковий)
        # ламала нульо-аргументний виклик TypeError-ом; нічого в цій кодовій
        # базі так не кликало, але контракт супертипу мовчки порушувався.
        if idx is None:
            return super().parent()
        if not idx.isValid() or idx.internalId() == 0:
            return QModelIndex()
        gi = int(idx.internalId()) - 1
        row = self._group_pos.get(gi)
        return self.createIndex(row, 0, 0) if row is not None else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return self._root_fetched + int(
                self._root_fetched < len(self._group_order))
        if parent.internalId() != 0:
            return 0
        # Той самий подвійний виклик root_id, що й у index()
        # вище — одне обчислення замість двох.
        gi = self.root_id(parent.row())
        if gi is None:
            return 0
        fetched = self._fetched[gi]
        return fetched + int(fetched < len(self._child_order[gi]))

    def canFetchMore(self, parent=QModelIndex()):
        # QTreeView repeatedly calls fetchMore until exhaustion as soon as a
        # parent expands. On 100k rows that defeats pagination and freezes the
        # GUI. Further pages are exposed by an explicit sentinel row instead.
        return False

    def fetchMore(self, parent=QModelIndex()):
        if not parent.isValid():
            self._load_more_roots()
            return
        if not parent.isValid() or parent.internalId() != 0:
            return
        gi = self.root_id(parent.row())
        if gi is None or self._fetched[gi] >= len(self._child_order[gi]):
            return
        old = self._fetched[gi]
        new = min(old + PAGE_SIZE, len(self._child_order[gi]))
        old_count = old + 1  # real rows + the existing sentinel
        new_count = new + int(new < len(self._child_order[gi]))
        inserted = new_count - old_count
        if inserted:
            self.beginInsertRows(parent, old + 1, new_count - 1)
        self._fetched[gi] = new
        if inserted:
            self.endInsertRows()
        # The previous sentinel row is now the first row of the new page.
        self.dataChanged.emit(
            self.index(old, 0, parent),
            self.index(old, len(COLS) - 1, parent),
        )

    def _load_more_roots(self) -> bool:
        if self._root_fetched >= len(self._group_order):
            return False
        old = self._root_fetched
        new = min(old + ROOT_PAGE_SIZE, len(self._group_order))
        old_count = old + 1
        new_count = new + int(new < len(self._group_order))
        inserted = new_count - old_count
        if inserted:
            self.beginInsertRows(QModelIndex(), old + 1, new_count - 1)
        self._root_fetched = new
        if inserted:
            self.endInsertRows()
        self.dataChanged.emit(
            self.index(old, 0),
            self.index(old, len(COLS) - 1),
        )
        return True

    def load_more_at(self, idx: QModelIndex) -> bool:
        """Load one explicit page when the sentinel child is activated."""
        if not idx.isValid():
            return False
        if idx.internalId() == 0:
            if idx.row() != self._root_fetched:
                return False
            return self._load_more_roots()
        gi = int(idx.internalId()) - 1
        if not 0 <= gi < len(self._fetched) or idx.row() != self._fetched[gi]:
            return False
        parent_row = self._group_pos.get(gi)
        if parent_row is None or self._fetched[gi] >= len(self._child_order[gi]):
            return False
        self.fetchMore(self.index(parent_row, 0))
        return True

    def columnCount(self, parent=QModelIndex()):
        return len(COLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        return COLS[s] if o == Qt.Horizontal and role == Qt.DisplayRole else None

    def _path(self, idx) -> str | None:
        if idx.internalId() == 0:
            return None
        gi = int(idx.internalId()) - 1
        if (not 0 <= gi < len(self.groups)
                or idx.row() >= self._fetched[gi]
                or not 0 <= idx.row() < len(self._child_order[gi])):
            return None
        ri = self._child_order[gi][idx.row()]
        return self.groups[gi].paths[ri]

    def flags(self, idx):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if self._path(idx) is not None and idx.column() == 0:
            return base | Qt.ItemIsUserCheckable
        return base

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        if idx.internalId() == 0:
            if idx.row() == self._root_fetched < len(self._group_order):
                remaining = len(self._group_order) - self._root_fetched
                if role == Qt.DisplayRole and idx.column() == 0:
                    return (
                        f"Показати ще {min(ROOT_PAGE_SIZE, remaining)} груп · "
                        f"лишилось {remaining}")
                if role == Qt.ToolTipRole:
                    return "Завантажити наступну сторінку груп"
                if role == Qt.ForegroundRole:
                    return QColor("#4477aa")
                return None
            if not 0 <= idx.row() < self._root_fetched:
                return None
            g = self.groups[self._group_order[idx.row()]]
            if role == Qt.DisplayRole and idx.column() == 0:
                fam = getattr(g, "families", 0)
                clone_note = ("  ·  спільне сховище (клони)"
                              if fam and fam < len(g.paths) else "")
                return (f"Група {idx.row() + 1}  ·  копій: {len(g.paths)}"
                        f"  ·  звільнить до {human(g.wasted)}{clone_note}  ·  🛡")
            if role == Qt.ToolTipRole:
                return ("🛡 Перед переміщенням у Кошик DupScan повністю "
                        "перечитає вибраний елемент і незалежну копію.")
            if role == Qt.ForegroundRole:
                return QColor("#888888")
            if role == Qt.UserRole:
                return g.wasted
            return None
        gi = int(idx.internalId()) - 1
        if not 0 <= gi < len(self.groups):
            return None
        if idx.row() == self._fetched[gi] < len(self._child_order[gi]):
            remaining = len(self._child_order[gi]) - self._fetched[gi]
            if role == Qt.DisplayRole and idx.column() == 0:
                return f"Показати ще {min(PAGE_SIZE, remaining)} · лишилось {remaining}"
            if role == Qt.ToolTipRole:
                return "Завантажити наступну сторінку рядків цієї групи"
            if role == Qt.ForegroundRole:
                return QColor("#4477aa")
            return None
        if not 0 <= idx.row() < self._fetched[gi]:
            return None
        ri = self._child_order[gi][idx.row()]
        p = self._path(idx)
        if role == Qt.DisplayRole:
            if idx.column() in (2, 3) and self._display[gi][ri][idx.column()] is None:
                ns = self._sort_keys[gi][ri][idx.column()]
                self._display[gi][ri][idx.column()] = when(int(ns))
            shown = self._display[gi][ri][idx.column()]
            tag = self.tags.get(p) if p else None
            return f"{shown} · {tag}" if tag and idx.column() == 0 else shown
        if role == Qt.UserRole:  # ключ сортування
            return self._sort_keys[gi][ri][idx.column()]
        if role == Qt.CheckStateRole and idx.column() == 0:
            return Qt.Checked if p in self.checked else Qt.Unchecked
        if role == Qt.ToolTipRole:
            tag = self.tags.get(p) if p else None
            return f"{p}\nМітка: {tag}" if tag else p
        return None

    def setData(self, idx, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole:
            return False
        p = self._path(idx)
        if p is None:
            return False
        g = self.groups[int(idx.internalId()) - 1]
        wants_checked = value == Qt.Checked or value == 2
        is_checked = p in self.checked
        if wants_checked == is_checked:
            return True
        if wants_checked:
            # Тека-еталон: позначка на захищеному шляху не ставиться
            # взагалі — перший шар до пайплайна і to_trash-рубежа. Хук
            # інжектиться застосунком: модель не залежить від preferences
            # (як warn).
            checker = getattr(self, "protected_checker", None)
            if checker is not None and checker(p):
                self.warn(
                    "Шлях лежить у теці-еталоні — недоторканний за "
                    "визначенням. Зніміть позначку еталона, якщо це свідомо.")
                return False
            if sum(1 for q in g.paths if q in self.checked) >= len(g.paths) - 1:
                self.warn("У групі мусить лишитися щонайменше один непозначений елемент.")
                return False
            self.checked.add(p)
        else:
            self.checked.discard(p)
        self.dataChanged.emit(idx, idx, [Qt.CheckStateRole])
        self.checksChanged.emit()
        return True

    def _emit_checks_changed(self, group_ids=None) -> None:
        """Оновити ЛИШЕ чекбокси: dataChanged по дітях кожної групи.
        layoutChanged тут не можна — view губить стан розгортання груп."""
        ids = range(len(self.groups)) if group_ids is None else set(group_ids)
        for gi in ids:
            if not 0 <= gi < len(self.groups):
                continue
            parent_row = self.root_row(gi)
            if parent_row is not None and self._fetched[gi]:
                parent = self.index(parent_row, 0)
                self.dataChanged.emit(self.index(0, 0, parent),
                                      self.index(self._fetched[gi] - 1, 0, parent),
                                      [Qt.CheckStateRole])

    def set_tag(self, path: str, tag: str) -> None:
        """Мітка статусу рядка: лише view-шар,
        ScanResult/сесію не чіпає. За зразком SimModel.set_tag, але ключ —
        сам шлях (рядок групи однозначно ідентифікується ним, на відміну
        від пари A/B)."""
        if tag:
            self.tags[path] = tag
        else:
            self.tags.pop(path, None)
        for gi, g in enumerate(self.groups):
            if path not in g.paths:
                continue
            parent_row = self.root_row(gi)
            if parent_row is None or gi >= len(self._child_order):
                return
            ri = g.paths.index(path)
            if ri not in self._child_order[gi]:
                return
            row = self._child_order[gi].index(ri)
            if row >= self._fetched[gi]:
                return
            parent = self.index(parent_row, 0)
            self.dataChanged.emit(
                self.index(row, 0, parent),
                self.index(row, len(COLS) - 1, parent),
                [Qt.DisplayRole, Qt.ToolTipRole])
            return

    def _survivor(self, gi: int, candidates=None) -> str | None:
        """Choose one safe survivor, preferring an explicitly scoped path."""
        paths = self.groups[gi].paths
        if not paths:
            return None
        if candidates is not None:
            candidates = list(candidates)
        if self.keeper_fn is not None:
            keep = self.keeper_fn(paths)
            if keep in paths and (candidates is None or keep in candidates):
                return keep
        if candidates:
            return min(candidates, key=lambda p: self._path_mtime(p))
        if self.keeper_fn is not None:
            keep = self.keeper_fn(paths)
            if keep in paths:
                return keep
        return min(paths, key=self._path_mtime)

    def _path_mtime(self, path: str) -> int:
        dates = self.dates_fn(path)
        return dates[1] if dates else 0

    def _select_scope_safe(self, scoped: dict[int, list[str]]) -> None:
        """Select scopes without ever leaving an affected group fully checked."""
        changed = set()
        for gi, paths in scoped.items():
            if not paths or not 0 <= gi < len(self.groups):
                continue
            group_paths = self.groups[gi].paths
            valid = [path for path in paths if path in group_paths]
            if not valid:
                continue
            before_group = {
                path for path in group_paths if path in self.checked}
            valid_set = set(valid)
            if all(
                    path in self.checked or path in valid_set
                    for path in group_paths):
                # For a filtered action, retain a survivor from the visible
                # scope where possible: selections hidden by the filter are
                # not silently removed.
                survivor = self._survivor(gi, valid)
                if survivor is not None:
                    self.checked.discard(survivor)
                    valid = [path for path in valid if path != survivor]
            self.checked.update(valid)
            after_group = {
                path for path in group_paths if path in self.checked}
            if before_group != after_group:
                changed.add(gi)
        if changed:
            self._emit_checks_changed(changed)
            self.checksChanged.emit()

    def select_group_safe(self, group_id: int) -> None:
        """Select one whole group while preserving one keeper."""
        if not 0 <= group_id < len(self.groups):
            return
        self._select_scope_safe({group_id: list(self.groups[group_id].paths)})

    def select_filtered_safe(self) -> None:
        """Select all filtered children, including pages not loaded by the view."""
        self._select_scope_safe({
            gi: [self.groups[gi].paths[ri] for ri in rows]
            for gi in self._group_order
            for rows in (self._child_order[gi],) if rows
        })

    def select_all_safe(self) -> None:
        """Select every group safely, replacing the legacy all-duplicates action."""
        self.checked.clear()
        for gi, group in enumerate(self.groups):
            survivor = self._survivor(gi)
            self.checked.update(path for path in group.paths if path != survivor)
        self._emit_checks_changed()
        self.checksChanged.emit()

    def select_all_dups(self) -> None:
        """Backward-compatible name for the safe all-groups selection."""
        self.select_all_safe()

    def clear_filtered(self) -> None:
        """Clear only currently filtered paths; hidden selections remain intact."""
        scoped = {
            self.groups[gi].paths[ri]
            for gi in self._group_order for ri in self._child_order[gi]
        }
        affected = {
            gi for gi in self._group_order
            if any(self.groups[gi].paths[ri] in self.checked
                   for ri in self._child_order[gi])
        }
        if not (self.checked & scoped):
            return
        self.checked.difference_update(scoped)
        self._emit_checks_changed(affected)
        self.checksChanged.emit()

    def selection_stats(self, paths=None) -> dict[str, int]:
        """Return count, bytes and involved groups for selected or supplied paths."""
        wanted = self.checked if paths is None else set(paths)
        count = 0
        total = 0
        groups = 0
        for gi, group in enumerate(self.groups):
            matched = set(group.paths) & wanted
            if matched:
                groups += 1
                count += len(matched)
                total += len(matched) * group.size
        return {"count": count, "bytes": total, "groups": groups}

    def clear_checks(self) -> None:
        self.checked.clear()
        self._emit_checks_changed()
        self.checksChanged.emit()


class SimModel(QAbstractItemModel):
    """Пара подібності -> спільні файли. Чекбокси на ОБОХ боках рядка;
    інваріант: у рядку позначити можна максимум один бік — контент ніколи
    не зникає з обох тек одночасно."""

    SCOLS = ("A — тека / файл", "B — тека / файл", "Подібність", "Спільні дані")
    sortStarted = Signal()
    sortFinished = Signal()
    checksChanged = Signal()

    def __init__(self):
        super().__init__()
        self.pairs: list = []
        self.checked: set[str] = set()
        self.tags: dict[tuple[str, str], str] = {}
        self.warn = lambda msg: None
        # Інжектиться застосунком (тека-еталон); модель не знає preferences.
        self.protected_checker: Callable[[str], bool] | None = None
        # ліниво дообчислені diff-рядки: pi -> (rows_a, rows_b, na, nb);
        # None = сентинел «рахується у фоні»
        self.diff: dict[int, tuple | None] = {}
        self._rowmap: dict[int, list[tuple[str, int]]] = {}
        self._pair_order: list[int] = []
        self._all_pair_order: list[int] = []
        self._pair_pos: dict[int, int] = {}
        self._root_fetched = 0
        self._pair_display: list[tuple[str, str, str, str]] = []
        self._pair_keys: list[tuple[object, object, object, object]] = []
        self._cells: dict[tuple[int, str, int], tuple[
            tuple[str, str, str, str], tuple[object, object, object, object], str
        ]] = {}
        self._sort_column = -1
        self._sort_order = Qt.AscendingOrder
        self._filter_text = ""
        self._min_similarity = 0

    @staticmethod
    def prepare_pairs(pairs: list, cancel=None):
        display = []
        keys = []
        valid_paths: set[str] = set()
        for index, pair in enumerate(pairs):
            if cancel is not None and index % 1024 == 0 and cancel.is_set():
                return None
            display.append((
                f"A · {pair.dir_a}", f"B · {pair.dir_b}",
                f"{pair.percent:.1f} %", human(pair.shared_bytes),
            ))
            keys.append((
                pair.dir_a.casefold(), pair.dir_b.casefold(),
                pair.percent, pair.shared_bytes,
            ))
            for _size, path_a, path_b in pair.shared:
                valid_paths.add(path_a)
                valid_paths.add(path_b)
        return display, keys, valid_paths

    def apply_prepared_pairs(self, pairs: list, payload) -> None:
        self.beginResetModel()
        display, keys, valid_paths = payload
        self.pairs = pairs
        self.diff = {}
        self._rowmap = {}
        self._cells = {}
        self._pair_display = display
        self._pair_keys = keys
        self.checked.intersection_update(valid_paths)
        self._all_pair_order = list(range(len(pairs)))
        self._apply_sort_order()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._pair_order))
        self.endResetModel()
        self.checksChanged.emit()

    def set_pairs(self, pairs: list) -> None:
        payload = self.prepare_pairs(pairs)
        self.apply_prepared_pairs(pairs, payload)

    def _apply_sort_order(self) -> None:
        if self._sort_column >= 0:
            reverse = self._sort_order == Qt.DescendingOrder
            # _pair_keys навмисно tuple[object, object,
            # object, object] (рядок 810) — той самий різнотипний-стовпчик
            # інваріант, що GroupModel._sort_keys.
            self._all_pair_order.sort(
                key=lambda pi: self._pair_keys[pi][self._sort_column],  # type: ignore[arg-type, return-value]
                reverse=reverse)
            for pi, rows in self._rowmap.items():
                # pi=pi — навмисне раннє захоплення значення циклу (уникнути
                # класичного пізнього зв'язування в лямбді); mypy не виводить
                # тип лямбди з дефолтним параметром + *args.
                rows.sort(key=lambda desc, pi=pi: self._cell_sort(pi, *desc),  # type: ignore[misc]
                          reverse=reverse)
        if self._filter_text or self._min_similarity:
            self._pair_order = [
                pi for pi in self._all_pair_order
                if (not self._filter_text
                    or self._filter_text in self._pair_keys[pi][0]  # type: ignore[operator]
                    or self._filter_text in self._pair_keys[pi][1])  # type: ignore[operator]
                and self.pairs[pi].percent >= self._min_similarity
            ]
        else:
            self._pair_order = list(self._all_pair_order)
        self._pair_pos = {pi: row for row, pi in enumerate(self._pair_order)}

    def set_filter(self, text: str) -> None:
        query = text.strip().casefold()
        if query == self._filter_text:
            return
        self.beginResetModel()
        self._filter_text = query
        self._apply_sort_order()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._pair_order))
        self.endResetModel()

    def set_threshold(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        if percent == self._min_similarity:
            return
        self.beginResetModel()
        self._min_similarity = percent
        self._apply_sort_order()
        self._root_fetched = min(ROOT_PAGE_SIZE, len(self._pair_order))
        self.endResetModel()

    @staticmethod
    def pair_key(pair) -> tuple[str, str]:
        return tuple(sorted((pair.dir_a, pair.dir_b)))

    def set_tag(self, pair, tag: str) -> None:
        key = self.pair_key(pair)
        if tag:
            self.tags[key] = tag
        else:
            self.tags.pop(key, None)
        try:
            pi = self.pairs.index(pair)
        except ValueError:
            return
        row = self.root_row(pi)
        if row is not None:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.SCOLS) - 1),
                                  [Qt.DisplayRole, Qt.ToolTipRole])

    def sort(self, column, order=Qt.AscendingOrder):
        if not 0 <= column < len(self.SCOLS):
            return
        self.sortStarted.emit()
        self.layoutAboutToBeChanged.emit()
        self._sort_column = column
        self._sort_order = order
        self._apply_sort_order()
        self.layoutChanged.emit()
        self.sortFinished.emit()

    def root_id(self, row: int) -> int | None:
        return (
            self._pair_order[row]
            if 0 <= row < self._root_fetched else None
        )

    def root_row(self, item_id: int) -> int | None:
        row = self._pair_pos.get(item_id)
        return row if row is not None and row < self._root_fetched else None

    def _ensure_root_visible(self, item_id: int) -> int | None:
        row = self._pair_pos.get(item_id)
        if row is None:
            return None
        while row >= self._root_fetched:
            self._load_more_roots()
        return row

    def total_children(self) -> int:
        return sum(self._row_count_for(pi) for pi in range(len(self.pairs)))

    def item_key(self, idx):
        if not idx.isValid():
            return None
        if idx.internalId() == 0:
            pi = self.root_id(idx.row())
            return ("p", pi, idx.column()) if pi is not None else None
        pi = int(idx.internalId()) - 1
        rows = self._rows_for(pi) if 0 <= pi < len(self.pairs) else []
        if not 0 <= idx.row() < len(rows):
            return None
        return ("r", pi, rows[idx.row()], idx.column())

    def index_for_key(self, key):
        if not key:
            return QModelIndex()
        if key[0] == "p":
            row = self._ensure_root_visible(key[1])
            return self.index(row, key[2]) if row is not None else QModelIndex()
        if key[0] == "r" and 0 <= key[1] < len(self.pairs):
            try:
                row = self._rows_for(key[1]).index(key[2])
            except ValueError:
                return QModelIndex()
            parent_row = self._ensure_root_visible(key[1])
            if parent_row is not None:
                return self.index(row, key[3], self.index(parent_row, 0))
        return QModelIndex()

    def _truncated(self, pr) -> int:
        return max(0, getattr(pr, "shared_total", 0) - len(pr.shared))

    def _rows_for(self, pi: int) -> list[tuple[str, int]]:
        """Дескриптори дитячих рядків пари: (kind, i); kind ∈ s/ts/a/ta/b/tb."""
        rows = self._rowmap.get(pi)
        if rows is not None:
            return rows
        pr = self.pairs[pi]
        rows = [("s", i) for i in range(len(pr.shared))]
        if self._truncated(pr):
            rows.append(("ts", 0))
        d = self.diff.get(pi)
        if d:
            oa, ob, na, nb = d
            rows += [("a", i) for i in range(len(oa))]
            if na > len(oa):
                rows.append(("ta", 0))
            rows += [("b", i) for i in range(len(ob))]
            if nb > len(ob):
                rows.append(("tb", 0))
        if self._sort_column >= 0:
            reverse = self._sort_order == Qt.DescendingOrder
            rows.sort(key=lambda desc: self._cell_sort(pi, *desc),
                      reverse=reverse)
        self._rowmap[pi] = rows
        return rows

    def _row_count_for(self, pi: int) -> int:
        pr = self.pairs[pi]
        count = len(pr.shared) + int(bool(self._truncated(pr)))
        d = self.diff.get(pi)
        if d:
            oa, ob, na, nb = d
            count += len(oa) + len(ob)
            count += int(na > len(oa)) + int(nb > len(ob))
        return count

    def _cache_cell(self, pi: int, kind: str, i: int):
        key = (pi, kind, i)
        cached = self._cells.get(key)
        if cached is not None:
            return cached
        pr = self.pairs[pi]
        if kind == "s":
            size, fa, fb = pr.shared[i]
            ra, rb = os.path.relpath(fa, pr.dir_a), os.path.relpath(fb, pr.dir_b)
            cached = ((ra, rb, "спільний", human(size)),
                      (ra.casefold(), rb.casefold(), "спільний", size),
                      f"{fa}\n{fb}")
        elif kind in ("a", "b"):
            d = self.diff[pi]
            # Self.diff[pi] статично tuple | None (None —
            # сентинел "уже рахується у фоні", _sim_expanded). Але рядки
            # kind "a"/"b" потрапляють у _rowmap лише з _rows_for, яка сама
            # гейтить їх на `if d:` (сентинел падає) — інваріант, не
            # перевірка про всяк випадок.
            assert d is not None, "рядок diff-кластера без обчисленого diff"
            size, p = (d[0] if kind == "a" else d[1])[i]
            root = pr.dir_a if kind == "a" else pr.dir_b
            rel = os.path.relpath(p, root)
            label = "лише в A" if kind == "a" else "лише в B"
            shown = (rel, "—") if kind == "a" else ("—", rel)
            keys = (rel.casefold(), "") if kind == "a" else ("", rel.casefold())
            cached = ((*shown, label, human(size)),
                      (*keys, label, size),
                      f"{p}\nЄ лише на цьому боці — остання копія, "
                      "позначати не можна.")
        else:
            if kind == "ts":
                label = (f"… і ще {self._truncated(pr)} спільних "
                         f"(показано перші {len(pr.shared)})")
            else:
                d = self.diff[pi]
                # Той самий інваріант, що вище — kind "ta"/"tb"
                # теж лише з _rows_for, гейтованої на `if d:`.
                assert d is not None, "рядок diff-кластера без обчисленого diff"
                label = (f"… і ще {d[2] - len(d[0])} лише в A" if kind == "ta"
                         else f"… і ще {d[3] - len(d[1])} лише в B")
            cached = ((label, "", "", ""),
                      (label.casefold(), "", "", 0), "")
        self._cells[key] = cached
        return cached

    def _cell_sort(self, pi: int, kind: str, i: int):
        return self._cache_cell(pi, kind, i)[1][self._sort_column]

    def set_diff(self, pi: int, oa, ob, na: int, nb: int) -> None:
        """Вставити diff-рядки В КІНЕЦЬ дітей пари (розгортання/чекбокси живі)."""
        add = (len(oa) + (1 if na > len(oa) else 0)
               + len(ob) + (1 if nb > len(ob) else 0))
        if add == 0:
            self.diff[pi] = (oa, ob, na, nb)
            return
        old_rows = list(self._rows_for(pi))
        old = len(old_rows)
        parent_row = self.root_row(pi)
        if parent_row is None:
            self.diff[pi] = (oa, ob, na, nb)
            self._rowmap.pop(pi, None)
            return
        self.beginInsertRows(self.index(parent_row, 0), old, old + add - 1)
        self.diff[pi] = (oa, ob, na, nb)
        added = [("a", i) for i in range(len(oa))]
        if na > len(oa):
            added.append(("ta", 0))
        added += [("b", i) for i in range(len(ob))]
        if nb > len(ob):
            added.append(("tb", 0))
        self._rowmap[pi] = old_rows + added
        for kind, i in added:
            self._cache_cell(pi, kind, i)
        self.endInsertRows()
        if self._sort_column >= 0:
            self.sort(self._sort_column, self._sort_order)

    def index(self, row, col, parent=QModelIndex()):
        if not parent.isValid():
            return self.createIndex(row, col, 0)
        if parent.internalId() != 0:
            return QModelIndex()
        # Один виклик root_id — те саме, що GroupModel.index.
        pi = self.root_id(parent.row())
        if pi is None:
            return QModelIndex()
        return self.createIndex(row, col, pi + 1)

    def parent(self, idx=None):
        # Той самий фікс перевантаження, що й GroupModel.parent.
        if idx is None:
            return super().parent()
        if not idx.isValid() or idx.internalId() == 0:
            return QModelIndex()
        pi = int(idx.internalId()) - 1
        row = self.root_row(pi)
        return self.createIndex(row, 0, 0) if row is not None else QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return self._root_fetched + int(
                self._root_fetched < len(self._pair_order))
        if parent.internalId() != 0:
            return 0
        # Один виклик root_id — те саме, що GroupModel.rowCount.
        pi = self.root_id(parent.row())
        if pi is None:
            return 0
        return len(self._rows_for(pi))

    def _load_more_roots(self) -> bool:
        if self._root_fetched >= len(self._pair_order):
            return False
        old = self._root_fetched
        new = min(old + ROOT_PAGE_SIZE, len(self._pair_order))
        old_count = old + 1
        new_count = new + int(new < len(self._pair_order))
        inserted = new_count - old_count
        if inserted:
            self.beginInsertRows(QModelIndex(), old + 1, new_count - 1)
        self._root_fetched = new
        if inserted:
            self.endInsertRows()
        self.dataChanged.emit(
            self.index(old, 0),
            self.index(old, len(self.SCOLS) - 1),
        )
        return True

    def load_more_at(self, idx: QModelIndex) -> bool:
        if (not idx.isValid() or idx.internalId() != 0
                or idx.row() != self._root_fetched):
            return False
        return self._load_more_roots()

    def columnCount(self, parent=QModelIndex()):
        return len(self.SCOLS)

    def headerData(self, s, o, role=Qt.DisplayRole):
        return self.SCOLS[s] if o == Qt.Horizontal and role == Qt.DisplayRole else None

    def flags(self, idx):
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if idx.isValid() and idx.internalId() != 0 and idx.column() in (0, 1):
            rows = self._rows_for(int(idx.internalId()) - 1)
            # позначувані ЛИШЕ спільні рядки; only-рядки — остання копія
            if idx.row() < len(rows) and rows[idx.row()][0] == "s":
                return base | Qt.ItemIsUserCheckable
        return base

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        if idx.internalId() == 0:
            if idx.row() == self._root_fetched < len(self._pair_order):
                remaining = len(self._pair_order) - self._root_fetched
                if role == Qt.DisplayRole and idx.column() == 0:
                    return (
                        f"Показати ще {min(ROOT_PAGE_SIZE, remaining)} пар · "
                        f"лишилось {remaining}")
                if role == Qt.ToolTipRole:
                    return "Завантажити наступну сторінку подібних папок"
                if role == Qt.ForegroundRole:
                    return QColor("#4477aa")
                return None
            if not 0 <= idx.row() < self._root_fetched:
                return None
            pi = self._pair_order[idx.row()]
            p = self.pairs[pi]
            if role == Qt.DisplayRole:
                shown = self._pair_display[pi][idx.column()]
                tag = self.tags.get(self.pair_key(p))
                return f"{shown} · {tag}" if tag and idx.column() == 2 else shown
            if role == Qt.UserRole:
                return self._pair_keys[pi][idx.column()]
            if role == Qt.ToolTipRole:
                tag = self.tags.get(self.pair_key(p))
                return (f"{p.dir_a}\n{p.dir_b}\n"
                        + (f"Мітка: {tag}\n" if tag else "")
                        + "Розгорніть і позначте, що саме "
                        "і з якого боку прибрати.")
            return None
        pi = int(idx.internalId()) - 1
        pr = self.pairs[pi]
        rows = self._rows_for(pi)
        if idx.row() >= len(rows):
            return None
        kind, i = rows[idx.row()]
        if kind == "s":
            if role == Qt.DisplayRole:
                return self._cache_cell(pi, kind, i)[0][idx.column()]
            if role == Qt.UserRole:
                return self._cache_cell(pi, kind, i)[1][idx.column()]
            if role == Qt.CheckStateRole and idx.column() in (0, 1):
                _size, fa, fb = pr.shared[i]
                p = fa if idx.column() == 0 else fb
                return Qt.Checked if p in self.checked else Qt.Unchecked
            if role == Qt.ToolTipRole:
                return self._cache_cell(pi, kind, i)[2]
            return None
        if kind in ("a", "b"):
            if role == Qt.DisplayRole:
                return self._cache_cell(pi, kind, i)[0][idx.column()]
            if role == Qt.UserRole:
                return self._cache_cell(pi, kind, i)[1][idx.column()]
            if role == Qt.ToolTipRole:
                return self._cache_cell(pi, kind, i)[2]
            return None
        # службові рядки «…і ще N»
        if role == Qt.DisplayRole:
            return self._cache_cell(pi, kind, i)[0][idx.column()]
        if role == Qt.UserRole:
            return self._cache_cell(pi, kind, i)[1][idx.column()]
        if role == Qt.ForegroundRole:
            return QColor("#888888")
        return None

    def setData(self, idx, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or not idx.isValid() or idx.internalId() == 0:
            return False
        pi = int(idx.internalId()) - 1
        pr = self.pairs[pi]
        rows = self._rows_for(pi)
        if (idx.row() >= len(rows) or rows[idx.row()][0] != "s"
                or idx.column() not in (0, 1)):
            return False
        _size, fa, fb = pr.shared[rows[idx.row()][1]]
        p, other = (fa, fb) if idx.column() == 0 else (fb, fa)
        wants_checked = value == Qt.Checked or value == 2
        is_checked = p in self.checked
        if wants_checked == is_checked:
            return True
        if wants_checked:
            # Тека-еталон: той самий перший шар, що в GroupModel.setData.
            checker = getattr(self, "protected_checker", None)
            if checker is not None and checker(p):
                self.warn(
                    "Шлях лежить у теці-еталоні — недоторканний за "
                    "визначенням. Зніміть позначку еталона, якщо це свідомо.")
                return False
            if other in self.checked:
                self.warn("У рядку можна позначити лише один бік — "
                          "інша копія мусить лишитися.")
                return False
            self.checked.add(p)
        else:
            self.checked.discard(p)
        self.dataChanged.emit(idx, idx, [Qt.CheckStateRole])
        self.checksChanged.emit()
        return True

    def clear_checks(self) -> None:
        self.checked.clear()
        for pi, rows in self._rowmap.items():  # лише materialized checkboxes
            pr = self.pairs[pi]
            parent_row = self.root_row(pi)
            if parent_row is not None and pr.shared and rows:
                parent = self.index(parent_row, 0)
                self.dataChanged.emit(self.index(0, 0, parent),
                                      self.index(len(rows) - 1, 1, parent),
                                      [Qt.CheckStateRole])
        self.checksChanged.emit()

    def path_at(self, idx) -> str | None:
        if not idx.isValid():
            return None
        if idx.internalId() == 0:
            pi = self.root_id(idx.row())
            if pi is None:
                return None
            p = self.pairs[pi]
            return p.dir_a if idx.column() == 0 else p.dir_b
        pi = int(idx.internalId()) - 1
        rows = self._rows_for(pi)
        if idx.row() >= len(rows):
            return None
        kind, i = rows[idx.row()]
        pr = self.pairs[pi]
        if kind == "s":
            _size, fa, fb = pr.shared[i]
            return fa if idx.column() == 0 else fb
        if kind == "a" and idx.column() == 0:
            # Той самий інваріант, що в _cache_cell — kind
            # "a"/"b" лише з _rows_for, гейтованої на `if d:`.
            d = self.diff[pi]
            assert d is not None, "рядок diff-кластера без обчисленого diff"
            return d[0][i][1]
        if kind == "b" and idx.column() == 1:
            d = self.diff[pi]
            assert d is not None, "рядок diff-кластера без обчисленого diff"
            return d[1][i][1]
        return None


class ClusterModel(QAbstractItemModel):
    """Транзитивні кластери тек — read-only.

    Корінь-рядок = кластер, дочірні рядки = теки-члени. СТРУКТУРНО
    read-only: немає checked/set_tag/чекбоксів — жоден деструктивний шлях
    (delete_checked, _trash_current_duplicate,
    _verify_snapshot_group_then_trash, sim-шляхи злиття) не приймає цей
    тип, кожен явно відмовляє (перевірено тестами по одному на точку
    входу). Без пагінації — кластери за задумом невеликий, курований
    список (dub_finder: «LSH/minhash... кластери знімають головний біль
    top-400 дешевше»), не 100k-рядковий набір, як групи/пари.
    """

    COLS = ("Кластер", "Спільні байти", "Класів")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.clusters: list[clusters.DirCluster] = []

    def set_clusters(self, items: list) -> None:
        self.beginResetModel()
        self.clusters = list(items)
        self.endResetModel()

    def index(self, row, col, parent=QModelIndex()):
        if not parent.isValid():
            if 0 <= row < len(self.clusters):
                return self.createIndex(row, col, 0)
            return QModelIndex()
        if parent.internalId() != 0:
            return QModelIndex()
        gi = parent.row()
        if 0 <= gi < len(self.clusters) and 0 <= row < len(self.clusters[gi].dirs):
            return self.createIndex(row, col, gi + 1)
        return QModelIndex()

    def parent(self, idx=None):
        # Той самий фікс перевантаження, що GroupModel.parent.
        if idx is None:
            return super().parent()
        if not idx.isValid() or idx.internalId() == 0:
            return QModelIndex()
        gi = int(idx.internalId()) - 1
        if 0 <= gi < len(self.clusters):
            return self.createIndex(gi, 0, 0)
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self.clusters)
        if parent.internalId() != 0:
            return 0
        gi = parent.row()
        if 0 <= gi < len(self.clusters):
            return len(self.clusters[gi].dirs)
        return 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if 0 <= section < len(self.COLS):
                return self.COLS[section]
        return None

    def cluster_at(self, idx) -> "clusters.DirCluster | None":
        """Кластер для будь-якого рядка (кореня чи дочірнього) під *idx*."""
        if not idx.isValid():
            return None
        gi = idx.row() if idx.internalId() == 0 else int(idx.internalId()) - 1
        return self.clusters[gi] if 0 <= gi < len(self.clusters) else None

    def dir_at(self, idx) -> str | None:
        """Шлях теки, якщо *idx* — дочірній (не кореневий) рядок."""
        if not idx.isValid() or idx.internalId() == 0:
            return None
        cluster = self.cluster_at(idx)
        if cluster is None or not (0 <= idx.row() < len(cluster.dirs)):
            return None
        return cluster.dirs[idx.row()]

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid() or role != Qt.DisplayRole:
            return None
        cluster = self.cluster_at(idx)
        if cluster is None:
            return None
        col = idx.column()
        if idx.internalId() == 0:
            if col == 0:
                return f"{len(cluster.dirs)} тек"
            if col == 1:
                return human(cluster.size)
            if col == 2:
                return str(cluster.class_count)
            return None
        path = cluster.dirs[idx.row()]
        if col == 0:
            return path
        if col == 1:
            return f"{cluster.percent_for(path):.0f}% дублікатів у теці"
        if col == 2:
            return "—"
        return None


class PerceptualModel(QAbstractItemModel):
    """«Схожі фото (підказка)» — read-only.

    Корінь-рядок = група перцептивно схожих зображень, дочірні рядки —
    файли-члени. НАЙВАЖЛИВІШИЙ інваріант продукту: перцептивна група —
    ПІДКАЗКА, не доказ (немає точного контентного дайджеста), тож ЦЯ
    модель структурно НЕ МОЖЕ потрапити в деструктив — немає checked,
    немає set_tag, немає жодного поля, яке гейт Кошика міг би прийняти
    за позначку. Кожна деструктивна точка входу (app.py: delete_checked,
    _delete_duplicate_paths, _trash_current_duplicate,
    _verify_snapshot_group_then_trash) явно перевіряє тип і відмовляє —
    tests/test_perceptual_readonly_invariant.py, по тесту на кожну.
    """

    COLS = ("Файл", "Розмір")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups: list[perceptual.PerceptualGroup] = []

    def set_groups(self, items: list) -> None:
        self.beginResetModel()
        self.groups = list(items)
        self.endResetModel()

    def index(self, row, col, parent=QModelIndex()):
        if not parent.isValid():
            if 0 <= row < len(self.groups):
                return self.createIndex(row, col, 0)
            return QModelIndex()
        if parent.internalId() != 0:
            return QModelIndex()
        gi = parent.row()
        if 0 <= gi < len(self.groups) and 0 <= row < len(self.groups[gi].files):
            return self.createIndex(row, col, gi + 1)
        return QModelIndex()

    def parent(self, idx=None):
        # Той самий фікс перевантаження, що GroupModel.parent.
        if idx is None:
            return super().parent()
        if not idx.isValid() or idx.internalId() == 0:
            return QModelIndex()
        gi = int(idx.internalId()) - 1
        if 0 <= gi < len(self.groups):
            return self.createIndex(gi, 0, 0)
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return len(self.groups)
        if parent.internalId() != 0:
            return 0
        gi = parent.row()
        if 0 <= gi < len(self.groups):
            return len(self.groups[gi].files)
        return 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if 0 <= section < len(self.COLS):
                return self.COLS[section]
        return None

    def path_at(self, idx) -> str | None:
        """Шлях файла, якщо *idx* — дочірній (не кореневий) рядок."""
        if not idx.isValid() or idx.internalId() == 0:
            return None
        gi = int(idx.internalId()) - 1
        if not (0 <= gi < len(self.groups)):
            return None
        group = self.groups[gi]
        return group.files[idx.row()] if 0 <= idx.row() < len(group.files) else None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid() or role != Qt.DisplayRole:
            return None
        col = idx.column()
        if idx.internalId() == 0:
            gi = idx.row()
            if not (0 <= gi < len(self.groups)):
                return None
            group = self.groups[gi]
            if col == 0:
                return f"{group.count} схожих фото"
            if col == 1:
                return "—"
            return None
        path = self.path_at(idx)
        if path is None:
            return None
        if col == 0:
            return path
        if col == 1:
            try:
                return human(os.path.getsize(path))
            except OSError:
                return "—"
        return None


@dataclass
class ProblemCategory:
    """Один top-level рядок ProblemsModel: категорія `product.classify_problem`.

    НЕ FileGroup/DirGroup: немає checked/жодного поля, яке деструктивний
    код міг би прийняти за позначку — той самий структурний прийом, що
    ClusterModel і PerceptualModel уже застосовують
    вище в цьому файлі.
    """

    title: str
    advice: str
    messages: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.messages)


class ProblemsModel(QAbstractItemModel):
    """Вкладка «Проблеми» — read-only поверхня над
    тими самими помилками сканування, що вже показує ProblemsDialog
    (product.classify_problem) — жодної нової логіки групування, лише
    інша поверхня для тих самих даних.

    СТРУКТУРНО read-only, як ClusterModel/PerceptualModel: немає
    checked/set_tag/чекбоксів, тож жодна деструктивна точка входу
    (delete_checked, _delete_duplicate_paths,
    _verify_snapshot_group_then_trash, _trash_current_duplicate) не
    приймає цей тип — кожна явно відмовляє (перевірено тестами, по
    тесту на кожну, tests/test_problems_tab.py).

    Корінь-рядок = категорія (title з classify_problem), дочірні рядки —
    окремі повідомлення тієї категорії. Без пагінації — як кластери тек,
    курований список категорій, не 100k-рядковий набір результатів
    (деталі самі вже обрізані на MAX_ERROR_DETAILS у core.py).
    """

    COLS = ("Проблема", "Кількість", "Порада")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.categories: list[ProblemCategory] = []
        self._truncated_caption: str | None = None

    def set_errors(self, errors: list[str], total: int) -> None:
        """Перебудувати модель за списком повідомлень.

        Групує через product.classify_problem (та сама функція, що й
        ProblemsDialog) — детермінований порядок: спадання лічильника
        категорії, потім за назвою. *total* — повна кількість проблем
        (result.errors_total), може перевищувати len(errors), якщо
        деталі обрізані (MAX_ERROR_DETAILS у core.py); тоді додається
        окремий підпис-рядок «Показано N із M».
        """
        self.beginResetModel()
        grouped: dict[str, ProblemCategory] = {}
        for message in errors:
            title, advice = product.classify_problem(message)
            category = grouped.get(title)
            if category is None:
                category = ProblemCategory(title=title, advice=advice)
                grouped[title] = category
            category.messages.append(message)
        self.categories = sorted(
            grouped.values(), key=lambda c: (-len(c.messages), c.title))
        shown = len(errors)
        total = max(shown, total)
        self._truncated_caption = (
            f"Показано {shown:,} із {total:,}" if total > shown else None)
        self.endResetModel()

    def _root_count(self) -> int:
        return len(self.categories) + (1 if self._truncated_caption else 0)

    def index(self, row, col, parent=QModelIndex()):
        if not parent.isValid():
            if 0 <= row < self._root_count():
                return self.createIndex(row, col, 0)
            return QModelIndex()
        if parent.internalId() != 0:
            return QModelIndex()
        gi = parent.row()
        if 0 <= gi < len(self.categories) and 0 <= row < len(self.categories[gi].messages):
            return self.createIndex(row, col, gi + 1)
        return QModelIndex()

    def parent(self, idx=None):
        # Той самий фікс перевантаження, що GroupModel.parent.
        if idx is None:
            return super().parent()
        if not idx.isValid() or idx.internalId() == 0:
            return QModelIndex()
        gi = int(idx.internalId()) - 1
        if 0 <= gi < len(self.categories):
            return self.createIndex(gi, 0, 0)
        return QModelIndex()

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return self._root_count()
        if parent.internalId() != 0:
            return 0
        gi = parent.row()
        if 0 <= gi < len(self.categories):
            return len(self.categories[gi].messages)
        return 0

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if 0 <= section < len(self.COLS):
                return self.COLS[section]
        return None

    def message_at(self, idx) -> str | None:
        """Текст помилки, якщо *idx* — дочірній (не кореневий) рядок.

        None і для категорій, і для підпису-обрізки — ПКМ-меню на них
        не пропонує «Показати у Finder»/«Копіювати повідомлення»."""
        if not idx.isValid() or idx.internalId() == 0:
            return None
        gi = int(idx.internalId()) - 1
        if not (0 <= gi < len(self.categories)):
            return None
        category = self.categories[gi]
        return (category.messages[idx.row()]
                if 0 <= idx.row() < len(category.messages) else None)

    def category_at(self, idx) -> "ProblemCategory | None":
        """Категорія для будь-якого рядка (кореня чи дочірнього) під *idx*
        — той самий прийом, що ClusterModel.cluster_at вище в цьому
        файлі. None і для невалідного idx, і для підпису-рядка обрізки
        (він не категорія) — автофікс імен використовує це, щоб
        визначити, чи дозволена дія «Полагодити ім'я» для активного
        рядка/категорії (лише «Драйвер не відкриває файл»/«Елемент
        зник»)."""
        if not idx.isValid():
            return None
        gi = idx.row() if idx.internalId() == 0 else int(idx.internalId()) - 1
        return self.categories[gi] if 0 <= gi < len(self.categories) else None

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid() or role != Qt.DisplayRole:
            return None
        col = idx.column()
        if idx.internalId() == 0:
            gi = idx.row()
            if gi == len(self.categories) and self._truncated_caption is not None:
                return self._truncated_caption if col == 0 else None
            if not (0 <= gi < len(self.categories)):
                return None
            category = self.categories[gi]
            if col == 0:
                return category.title
            if col == 1:
                return str(category.count)
            if col == 2:
                return category.advice
            return None
        message = self.message_at(idx)
        if message is None:
            return None
        if col == 0:
            return message
        return "—" if col in (1, 2) else None
