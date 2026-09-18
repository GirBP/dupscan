"""Вкладка «Проблеми» для головного вікна, разом з масовим полагодженням
імен (NFC) у категорії.

Співробітник Main: не знає нічого про сканування чи сесії, лише про
модель проблем, статус-рядок і функції зворотного виклику (фонова
робота, відкриття шляху з урахуванням сесії), які отримує в
конструкторі.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QTreeView, QVBoxLayout, QWidget,
)

import dupscan.infra.fsops as fsops

if TYPE_CHECKING:
    from dupscan.ui.table_models import ProblemsModel


class ProblemsTabController:
    """Помилки сканування, згруповані за типом — read-only перелік плюс
    ПКМ-полагодження імені (NFC) на одному рядку чи масово за категорією."""

    def __init__(
        self,
        parent: QWidget,
        model: "ProblemsModel",
        status: QLabel,
        bg_run: Callable[..., None],
        open_result_path: Callable[[str, Callable], None],
        model_views: dict[object, QTreeView],
        view_names: dict[QTreeView, str],
        column_base_widths: dict[QTreeView, list[int]],
    ) -> None:
        self._parent = parent
        self._model = model
        self._status = status
        self._bg_run = bg_run
        self._open_result_path = open_result_path
        self.view = self._build_view(model_views, view_names, column_base_widths)
        self.tab = self._build_tab(self.view)

    def _build_view(
        self,
        model_views: dict[object, QTreeView],
        view_names: dict[QTreeView, str],
        column_base_widths: dict[QTreeView, list[int]],
    ) -> QTreeView:
        """Своя, простіша обв'язка — той самий підхід, що вкладки кластерів/
        перцептиву: read-only, без sortStarted/set_expanded/load_more_at,
        які має лише GroupModel/SimModel через Main._tree()."""
        view = QTreeView()
        view.setModel(self._model)
        view.setUniformRowHeights(True)
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.header().setStretchLastSection(False)
        model_views[self._model] = view
        view_names[view] = "problems"
        column_base_widths[view] = [
            view.header().sectionSize(c)
            for c in range(self._model.columnCount())
        ]
        return view

    def _build_tab(self, view: QTreeView) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        note = QLabel(
            "Помилки сканування, згруповані за типом. Нечитабельне не "
            "потрапляє в групи дублікатів — fail-closed, щоб не пропустити "
            "непідтверджений дублікат. Звідси нічого не видаляється.")
        note.setWordWrap(True)
        v.addWidget(note)
        toolbar = QHBoxLayout()
        # Увімкнена лише коли синій поточний рядок — у
        # категорії «Драйвер не відкриває файл»/«Елемент зник»
        # (update_namefix_button, на currentChanged нижче).
        self.b_namefix_bulk = QPushButton("Полагодити імена категорії…")
        self.b_namefix_bulk.setToolTip(
            "Спробувати перейменувати кожен рядок активної категорії на "
            "канонічний NFC. Наявні цілі ніколи не перезаписуються.")
        self.b_namefix_bulk.setEnabled(False)
        self.b_namefix_bulk.clicked.connect(self.namefix_bulk)
        toolbar.addWidget(self.b_namefix_bulk)
        toolbar.addStretch(1)
        v.addLayout(toolbar)
        self.l_status = QLabel("Проблем не виявлено.")
        self.l_status.setWordWrap(True)
        v.addWidget(self.l_status)
        v.addWidget(view, 1)
        view.selectionModel().currentChanged.connect(
            lambda _current, _previous: self.update_namefix_button())
        return w

    def menu(self, pos) -> None:
        from dupscan.ui import app as app_module
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        self.view.setCurrentIndex(idx)
        message = self._model.message_at(idx)
        path = (
            app_module._first_path_in_message(message)
            if message is not None else None)
        category = self._model.category_at(idx)
        namefix_enabled = bool(
            path is not None and category is not None
            and category.title in app_module._NAMEFIX_ELIGIBLE_CATEGORIES)
        menu = app_module.QMenu(self._parent)
        reveal_action = menu.addAction("Показати у Finder")
        reveal_action.setEnabled(path is not None)
        copy_action = menu.addAction("Копіювати повідомлення")
        copy_action.setEnabled(message is not None)
        namefix_action = menu.addAction("Спробувати полагодити ім'я (NFC)")
        namefix_action.setEnabled(namefix_enabled)
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen is reveal_action and path is not None:
            self._open_result_path(path, app_module.reveal)
        elif chosen is copy_action and message is not None:
            QApplication.clipboard().setText(message)
        elif chosen is namefix_action and namefix_enabled and path is not None:
            self.fix_name_at_path(path)

    def fix_name_at_path(self, path: str) -> None:
        """ПКМ «Спробувати полагодити ім'я (NFC)» — одна спроба, через
        bg_run: fsops.fix_name_to_nfc сам читає диск (preferences +
        renamex_np) — жодного з них не можна викликати з GUI-потоку."""
        from dupscan.ui import app as app_module
        dirpath = os.path.dirname(path)
        name = os.path.basename(path)
        self._status.setText(f"Пробую полагодити ім'я: {name}…")

        def job():
            return fsops.fix_name_to_nfc(dirpath, name)

        def done(result) -> None:
            outcome, detail = result
            self._status.setText(app_module._namefix_status_text(outcome, detail))

        self._bg_run(job, done)

    def update_namefix_button(self) -> None:
        from dupscan.ui import app as app_module
        if not hasattr(self, "b_namefix_bulk"):
            return  # під час побудови вкладки кнопка ще не створена
        category = self._model.category_at(self.view.currentIndex())
        eligible = bool(
            category is not None
            and category.title in app_module._NAMEFIX_ELIGIBLE_CATEGORIES)
        self.b_namefix_bulk.setEnabled(eligible)

    def namefix_bulk(self) -> None:
        """«Полагодити імена категорії…»: усі рядки АКТИВНОЇ категорії (де
        стоїть синій поточний рядок) з видобутим шляхом — best-effort,
        підсумок за outcome. Увесь цикл fix_name_to_nfc — у фоновому
        потоці (bg_run); GUI-потік лише читає готовий словник лічильників.
        """
        from dupscan.ui import app as app_module
        category = self._model.category_at(self.view.currentIndex())
        if (category is None
                or category.title not in app_module._NAMEFIX_ELIGIBLE_CATEGORIES):
            return
        items: list[tuple[str, str]] = []
        for message in category.messages:
            path = app_module._first_path_in_message(message)
            if path is not None:
                items.append((os.path.dirname(path), os.path.basename(path)))
        if not items:
            QMessageBox.information(
                self._parent, "DupScan",
                f"У категорії «{category.title}» немає рядків із "
                "видобутим шляхом.")
            return
        answer = QMessageBox.question(
            self._parent, "Полагодити імена категорії?",
            f"У категорії «{category.title}»: {len(items)} файл(ів) із "
            "видобутим шляхом. Спробувати перейменувати кожен на "
            "канонічний NFC? Наявні цілі НІКОЛИ не перезаписуються — на "
            "колізії файл лишається як є.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._status.setText(f"Полагоджую імена: {len(items)} файл(ів)…")

        def job() -> dict[str, int]:
            counts: dict[str, int] = {}
            for dirpath, name in items:
                try:
                    outcome, _detail = fsops.fix_name_to_nfc(dirpath, name)
                except Exception:  # noqa: BLE001 — один провал не зупиняє решту
                    outcome = "error"
                counts[outcome] = counts.get(outcome, 0) + 1
            return counts

        def done(counts: dict[str, int]) -> None:
            self._status.setText("Полагодження імен завершено.")
            summary = (
                f"Полагоджено {counts.get('fixed', 0)} · "
                f"фантомів {counts.get('phantom', 0)} · "
                f"існують {counts.get('exists', 0)} · "
                f"еталон {counts.get('protected', 0)} · "
                f"помилок {counts.get('error', 0)}")
            already_nfc = counts.get("already-nfc", 0)
            if already_nfc:
                summary += f" · вже канонічних {already_nfc}"
            if counts.get("fixed", 0):
                summary += (
                    "\n\nПовторіть сканування, щоб побачити оновлені "
                    "результати.")
            QMessageBox.information(self._parent, "Підсумок полагодження імен", summary)

        self._bg_run(job, done)
