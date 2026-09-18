"""Вкладка «Кластери тек» для головного вікна.

Співробітник Main: не знає нічого про сканування чи інші вкладки, лише
про модель кластерів, вкладку «Подібність папок» (для навігації до пари)
і статус-рядок, які отримує в конструкторі. Будує власний віджет вкладки
й обробляє власне ПКМ-меню.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QAbstractItemView, QLabel, QTabWidget, QTreeView, QVBoxLayout, QWidget,
)

if TYPE_CHECKING:
    from dupscan.ui.table_models import ClusterModel, SimModel


class ClustersTabController:
    """Read-only перелік кластерів тек: лише перегляд, без позначок і
    видалення (перевірено окремими інваріантними тестами в Main)."""

    def __init__(
        self,
        parent: QWidget,
        model: "ClusterModel",
        sim_model: "SimModel",
        sim_view: QTreeView,
        tabs: QTabWidget,
        status: QLabel,
        model_views: dict[object, QTreeView],
        view_names: dict[QTreeView, str],
        column_base_widths: dict[QTreeView, list[int]],
    ) -> None:
        self._parent = parent
        self._model = model
        self._sim_model = sim_model
        self._sim_view = sim_view
        self._tabs = tabs
        self._status = status
        self.view = self._build_view(model_views, view_names, column_base_widths)
        self.tab = self._build_tab(self.view)

    def _build_view(
        self,
        model_views: dict[object, QTreeView],
        view_names: dict[QTreeView, str],
        column_base_widths: dict[QTreeView, list[int]],
    ) -> QTreeView:
        """Своя, простіша обв'язка — без пагінації, без чекбоксів,
        без sortStarted/set_expanded/load_more_at, які має лише GroupModel/
        SimModel через Main._tree()."""
        view = QTreeView()
        view.setModel(self._model)
        view.setUniformRowHeights(True)
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.header().setStretchLastSection(False)
        model_views[self._model] = view
        view_names[view] = "clusters"
        column_base_widths[view] = [
            view.header().sectionSize(c)
            for c in range(self._model.columnCount())
        ]
        view.activated.connect(self.reveal_dir)
        return view

    def _build_tab(self, view: QTreeView) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        note = QLabel(
            "Кластери тек: теки, поєднані спільними дубльованими файлами "
            "(транзитивно — A-B одним файлом, B-C іншим об'єднує всі три). "
            "Лише довідково — ПКМ на теці: «Показати у Finder» або «Знайти "
            "пару в Подібності». Жодних позначок і видалення звідси.")
        note.setWordWrap(True)
        v.addWidget(note)
        v.addWidget(view, 1)
        return w

    def reveal_dir(self, idx) -> None:
        path = self._model.dir_at(idx)
        if path is not None:
            from dupscan.ui import app as app_module
            app_module.reveal(path)

    def menu(self, pos) -> None:
        from dupscan.ui import app as app_module
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        self.view.setCurrentIndex(idx)
        path = self._model.dir_at(idx)
        menu = app_module.QMenu(self._parent)
        reveal_action = menu.addAction("Показати у Finder")
        reveal_action.setEnabled(path is not None)
        find_pair_action = menu.addAction("Знайти пару в Подібності")
        find_pair_action.setEnabled(path is not None)
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if path is None or chosen is None:
            return
        if chosen is reveal_action:
            app_module.reveal(path)
        elif chosen is find_pair_action:
            self.find_pair_for_cluster_dir(path)

    def find_pair_for_cluster_dir(self, path: str) -> None:
        """Навігація на наявну вкладку «Подібність папок» — best-effort:
        кластер (спільний ФАЙЛ) і пара (поріг Dice-подібності ЦІЛОЇ теки)
        — різні відношення, збігу може не бути."""
        for pi, pair in enumerate(self._sim_model.pairs):
            if path in (pair.dir_a, pair.dir_b):
                self._tabs.setCurrentIndex(2)
                sim_idx = self._sim_model.index_for_key(("p", pi, 0))
                if sim_idx.isValid():
                    self._sim_view.setCurrentIndex(sim_idx)
                    self._sim_view.scrollTo(sim_idx)
                return
        self._status.setText(
            f"Немає відповідної пари в «Подібність папок» для: {path}")
