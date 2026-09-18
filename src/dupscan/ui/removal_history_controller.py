"""Журнал видалень і відновлення для головного вікна.

Співробітник Main: не знає нічого про вкладки чи сканування, лише про
батьківський віджет для діалогу й функції зворотного виклику (останні
скановані корені, фонова робота), які отримує в конструкторі.
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

import dupscan.infra.removal_history as removal_history
from dupscan.format import human, when


class RemovalHistoryController:
    """Діалог журналу видалень: перегляд операцій і безпечне відновлення."""

    def __init__(
        self,
        parent: QWidget,
        get_last_roots: Callable[[], Sequence[str]],
        ui_bg_run: Callable[..., None],
    ) -> None:
        self._parent = parent
        self._get_last_roots = get_last_roots
        self._ui_bg_run = ui_bg_run

    def show_removal_history(self) -> None:
        dialog = QDialog(self._parent)
        dialog.setWindowTitle("Видалення й відновлення — DupScan")
        dialog.resize(820, 540)
        layout = QVBoxLayout(dialog)
        info = QLabel("Завантажую журнал…")
        operations_list = QListWidget()
        items_list = QListWidget()
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(operations_list)
        splitter.addWidget(items_list)
        layout.addWidget(info)
        layout.addWidget(splitter, 1)
        row = QHBoxLayout()
        b_restore = QPushButton("Відновити вибране")
        b_trash = QPushButton("Відкрити Кошик")
        b_close = QPushButton("Закрити")
        row.addWidget(b_restore)
        row.addWidget(b_trash)
        row.addStretch(1)
        row.addWidget(b_close)
        layout.addLayout(row)

        def fill(operations):
            operations_list.clear()
            for operation in operations:
                restored = sum(item["status"] == "restored" for item in operation["items"])
                label = (f"{when(operation['created_ns'])} · {len(operation['items'])} "
                         f"елем. · {operation['status']}")
                if restored:
                    label += f" · відновлено {restored}"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, operation)
                operations_list.addItem(item)
            info.setText(
                f"Операцій: {len(operations)}. Автовідновлення доступне, коли "
                "macOS дозволила точно знайти елемент у Кошику."
                if operations else "Журнал видалень порожній.")
            if operations_list.count():
                operations_list.setCurrentRow(0)

        def show_items(current, _previous=None):
            items_list.clear()
            operation = current.data(Qt.ItemDataRole.UserRole) if current else None
            if not operation:
                return
            for value in operation["items"]:
                label = f"{value['status']} · {human(value['size'])} · {value['original_path']}"
                if (
                    value["status"] == "trashed"
                    and not removal_history.can_restore_automatically(value)
                ):
                    label += " · відновлення через Finder"
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, (operation["id"], value))
                items_list.addItem(item)

        def restore_current():
            current = items_list.currentItem()
            if not current:
                return
            operation_id, value = current.data(Qt.ItemDataRole.UserRole)
            if not removal_history.can_restore_automatically(value):
                QMessageBox.information(
                    dialog, "DupScan",
                    "Для цього історичного запису немає достатнього доказу "
                    "identity поточного елемента в Кошику. Відкрийте Кошик "
                    "і скористайтеся командою Finder «Повернути».")
                return
            allowed = tuple(self._get_last_roots())
            if not allowed:
                QMessageBox.information(
                    dialog, "DupScan",
                    "Спочатку додайте або проскануйте початкову теку, щоб "
                    "підтвердити безпечну область відновлення.")
                return
            info.setText("Безпечно відновлюю без перезапису…")

            def restored(_value):
                self._ui_bg_run(
                    lambda: removal_history.list_operations(limit=100), fill,
                    lambda error: info.setText(str(error)))

            self._ui_bg_run(
                lambda: removal_history.restore_item(
                    operation_id, value["original_path"], allowed_roots=allowed),
                restored,
                lambda error: QMessageBox.warning(dialog, "Не вдалося відновити", error),
            )

        operations_list.currentItemChanged.connect(show_items)
        b_restore.clicked.connect(restore_current)
        b_trash.clicked.connect(lambda: subprocess.Popen(
            ["open", os.path.expanduser("~/.Trash")]))
        b_close.clicked.connect(dialog.accept)
        self._ui_bg_run(
            lambda: removal_history.list_operations(limit=100), fill,
            lambda error: info.setText(f"Не вдалося прочитати журнал: {error}"))
        dialog.exec()
