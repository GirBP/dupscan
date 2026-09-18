"""Вкладка «Схожі фото (підказка)» для головного вікна.

Співробітник Main: не знає нічого про сканування інших вкладок, лише про
модель перцептивних груп, вкладки вікна (для назви табу) й функції
зворотного виклику для читання результату скану та стану воркера, які
отримує в конструкторі — воркер лишається станом Main (test-код і
_finish_model_refresh читають/пишуть main.perceptual_worker напряму).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTabWidget, QTreeView, QVBoxLayout, QWidget,
)

from dupscan.ui.workers import PerceptualScanWorker

if TYPE_CHECKING:
    import dupscan.domain.core as core
    import dupscan.ui.perceptual as perceptual
    from dupscan.ui.table_models import PerceptualModel


class PerceptualTabController:
    """«Схожі фото» — ПІДКАЗКА, не доказ: без позначок, без шляху в
    Кошик, окремий опційний воркер (наш core.scan фото не хешує
    перцептивно)."""

    def __init__(
        self,
        parent: QWidget,
        model: "PerceptualModel",
        tabs: QTabWidget,
        get_result: Callable[[], "core.ScanResult | None"],
        get_worker: Callable[[], "PerceptualScanWorker | None"],
        set_worker: Callable[["PerceptualScanWorker | None"], None],
        set_scanned_for: Callable[["core.ScanResult | None"], None],
        model_views: dict[object, QTreeView],
        view_names: dict[QTreeView, str],
        column_base_widths: dict[QTreeView, list[int]],
    ) -> None:
        self._parent = parent
        self._model = model
        self._tabs = tabs
        self._get_result = get_result
        self._get_worker = get_worker
        self._set_worker = set_worker
        self._set_scanned_for = set_scanned_for
        self.view = self._build_view(model_views, view_names, column_base_widths)
        self.tab = self._build_tab(self.view)

    def _build_view(
        self,
        model_views: dict[object, QTreeView],
        view_names: dict[QTreeView, str],
        column_base_widths: dict[QTreeView, list[int]],
    ) -> QTreeView:
        """Своя, простіша обв'язка — той самий підхід, що вкладка кластерів."""
        view = QTreeView()
        view.setModel(self._model)
        view.setUniformRowHeights(True)
        view.setAlternatingRowColors(True)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.header().setStretchLastSection(False)
        model_views[self._model] = view
        view_names[view] = "perceptual"
        column_base_widths[view] = [
            view.header().sectionSize(c)
            for c in range(self._model.columnCount())
        ]
        view.activated.connect(self.quick_look)
        return view

    def _build_tab(self, view: QTreeView) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 8, 8, 8)
        note = QLabel(
            "Схожі фото — ПІДКАЗКА, не доказ: візуальна схожість "
            "(стиснуті/зменшені/переконвертовані копії), без точного "
            "контентного хешу. Тут немає позначок і немає шляху в Кошик — "
            "перевірте вручну через Quick Look чи Finder."
        )
        note.setWordWrap(True)
        v.addWidget(note)
        toolbar = QHBoxLayout()
        self.b_scan = QPushButton("Сканувати фото…")
        self.b_scan.setAccessibleName("Знайти візуально схожі зображення")
        self.b_scan.clicked.connect(self.scan)
        toolbar.addWidget(self.b_scan)
        self.b_pause = QPushButton("Пауза")
        self.b_pause.setCheckable(True)
        self.b_pause.setVisible(False)
        self.b_pause.toggled.connect(self.pause_toggle)
        toolbar.addWidget(self.b_pause)
        self.b_cancel = QPushButton("Скасувати")
        self.b_cancel.setVisible(False)
        self.b_cancel.clicked.connect(self.cancel)
        toolbar.addWidget(self.b_cancel)
        toolbar.addStretch(1)
        v.addLayout(toolbar)
        self.l_status = QLabel(
            "Натисніть «Сканувати фото…», щоб знайти візуально схожі "
            "зображення серед сканованих джерел.")
        self.l_status.setWordWrap(True)
        v.addWidget(self.l_status)
        v.addWidget(view, 1)
        return w

    def scan(self) -> None:
        result = self._get_result()
        if result is None or not result.scanned_roots:
            QMessageBox.information(
                self._parent, "DupScan", "Спершу виконайте сканування.")
            return
        if not result.live:
            # Перцептивні результати НЕ персистяться — historical
            # сесія не має дискового дерева, яке чесно можна пересканувати
            # тут же (те саме обмеження, що "ЛИШЕ ПЕРЕГЛЯД" для груп).
            QMessageBox.information(
                self._parent, "DupScan",
                "Завантажена сесія доступна лише для перегляду: "
                "перцептивні результати не зберігаються в сесії й не "
                "рахуються заново для історичного знімка. Виконайте нове "
                "сканування, щоб перевірити фото.")
            return
        worker = self._get_worker()
        if worker is not None and worker.isRunning():
            return
        self._model.set_groups([])
        self.b_scan.setEnabled(False)
        self.b_pause.setVisible(True)
        self.b_pause.setChecked(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setVisible(True)
        self.l_status.setText("Шукаю зображення…")
        worker = PerceptualScanWorker(list(result.scanned_roots))
        worker.progress.connect(self.progress)
        worker.done.connect(self.done)
        worker.failed.connect(self.failed)
        self._set_worker(worker)
        worker.start()

    def progress(self, phase: str, done: int, total: int) -> None:
        self.l_status.setText(f"{phase}: {done:,}/{total:,}" if total else phase)

    def pause_toggle(self, checked: bool) -> None:
        worker = self._get_worker()
        if worker is None:
            return
        if checked:
            worker.pause.set()
            self.b_pause.setText("Продовжити")
            self.l_status.setText("На паузі…")
        else:
            worker.pause.clear()
            self.b_pause.setText("Пауза")

    def cancel(self) -> None:
        worker = self._get_worker()
        if worker is not None:
            worker.cancel.set()
            worker.pause.clear()

    def reset_controls(self) -> None:
        self.b_scan.setEnabled(True)
        self.b_pause.setVisible(False)
        self.b_pause.setChecked(False)
        self.b_pause.setText("Пауза")
        self.b_cancel.setVisible(False)

    def done(self, result: "perceptual.PerceptualResult") -> None:
        self.reset_controls()
        self._set_worker(None)
        self._set_scanned_for(self._get_result())
        self._model.set_groups(result.groups)
        self._tabs.setTabText(4, f"Схожі фото (підказка) ({len(result.groups)})")
        err = f" · помилок: {len(result.errors)}" if result.errors else ""
        self.l_status.setText(
            f"Зображень перевірено: {result.images_hashed:,} · "
            f"груп схожості: {len(result.groups)}{err}")

    def failed(self, message: str) -> None:
        self.reset_controls()
        self._set_worker(None)
        self.l_status.setText(f"Помилка перцептивного скану: {message}")

    def quick_look(self, idx) -> None:
        path = self._model.path_at(idx)
        if path is not None:
            from dupscan.ui import app as app_module
            app_module.quick_look(path)

    def menu(self, pos) -> None:
        from dupscan.ui import app as app_module
        idx = self.view.indexAt(pos)
        if not idx.isValid():
            return
        self.view.setCurrentIndex(idx)
        path = self._model.path_at(idx)
        menu = app_module.QMenu(self._parent)
        quick_look_action = menu.addAction("Quick Look")
        quick_look_action.setEnabled(path is not None)
        reveal_action = menu.addAction("Показати у Finder")
        reveal_action.setEnabled(path is not None)
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if path is None or chosen is None:
            return
        if chosen is quick_look_action:
            app_module.quick_look(path)
        elif chosen is reveal_action:
            app_module.reveal(path)
