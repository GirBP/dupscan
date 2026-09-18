"""Звіти, діагностика й перевірка оновлень для головного вікна.

Співробітник Main: не знає нічого про вкладки чи сканування, лише про
кілька віджетів і функцій зворотного виклику, які отримує в конструкторі.
"""
from __future__ import annotations

import os
import subprocess
from typing import Callable, Collection, Sequence

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox, QWidget

import dupscan.domain.core as core
import dupscan.infra.diagnostics as diagnostics
import dupscan.infra.reports as reports
import dupscan.infra.updates as updates
from dupscan.format import human
from dupscan.version import VERSION


class ReportsController:
    """Експорт CSV/HTML-звіту, ZIP-архіву діагностики і перевірка оновлень."""

    def __init__(
        self,
        parent: QWidget,
        status: QLabel,
        settings: QSettings,
        get_result: Callable[[], "core.ScanResult | None"],
        get_selection_paths: Callable[[], Collection[str]],
        get_last_roots: Callable[[], Sequence[str]],
        ui_bg_run: Callable[..., None],
        read_crash_log: Callable[[], str],
    ) -> None:
        self._parent = parent
        self._status = status
        self._settings = settings
        self._get_result = get_result
        self._get_selection_paths = get_selection_paths
        self._get_last_roots = get_last_roots
        self._ui_bg_run = ui_bg_run
        self._read_crash_log = read_crash_log

    def export_report(self, kind: str) -> None:
        result = self._get_result()
        if result is None:
            QMessageBox.information(self._parent, "DupScan", "Спочатку виконайте сканування.")
            return
        extension = "csv" if kind == "csv" else "html"
        destination, _selected = QFileDialog.getSaveFileName(
            self._parent, "Експорт звіту",
            os.path.join(os.path.expanduser("~/Desktop"), f"DupScan-report.{extension}"),
            f"{extension.upper()} (*.{extension})")
        if not destination:
            return
        selected_paths = tuple(self._get_selection_paths())
        roots = tuple(self._get_last_roots())

        def job():
            if kind == "csv":
                return reports.export_csv(destination, result,
                                          selected_paths=selected_paths)
            return reports.export_html(
                destination, result, selected_paths=selected_paths,
                title="Звіт DupScan", roots=roots)

        def failed(error: str) -> None:
            self._status.setText("Звіт не створено; наявний файл не змінено.")
            QMessageBox.warning(self._parent, "Помилка звіту", error)

        self._status.setText("Створюю звіт у фоні…")
        self._ui_bg_run(
            job,
            lambda _summary: self._status.setText(f"Звіт збережено: {destination}"),
            failed,
        )

    def check_updates(self) -> None:
        manifest_url = updates.configured_manifest_url()
        if not manifest_url:
            QMessageBox.information(
                self._parent, "Оновлення DupScan",
                "Канал оновлень ще не прив’язано до публічного HTTPS manifest. "
                "Механізм готовий; для релізу потрібно вказати адресу каналу.")
            return
        self._status.setText("Перевіряю оновлення через захищений канал…")

        def done(result):
            if result.update_available:
                answer = QMessageBox.question(
                    self._parent, "Доступне оновлення",
                    f"Доступна версія {result.latest_version}. Відкрити сторінку релізу?")
                if answer == QMessageBox.StandardButton.Yes:
                    subprocess.Popen(["open", result.release_url])
            else:
                QMessageBox.information(
                    self._parent, "Оновлення DupScan", "У вас актуальна версія.")
            self._status.setText("Перевірку оновлень завершено.")

        self._ui_bg_run(
            lambda: updates.check_for_update(
                manifest_url, VERSION, enabled=True),
            done,
            lambda error: QMessageBox.warning(
                self._parent, "Не вдалося перевірити оновлення", error),
        )

    def export_diagnostics(self) -> None:
        destination, _selected = QFileDialog.getSaveFileName(
            self._parent, "Експорт діагностики",
            os.path.join(os.path.expanduser("~/Desktop"), "DupScan-diagnostics.zip"),
            "ZIP (*.zip)")
        if not destination:
            return
        answer = QMessageBox.question(
            self._parent, "Включити шляхи?",
            "Додати до діагностики список сканованих шляхів? Вміст файлів "
            "ніколи не додається. Оберіть «Ні» для максимальної приватності.")
        include_paths = answer == QMessageBox.StandardButton.Yes
        result = self._get_result()
        scanned = tuple(result.file_meta) if result else ()
        errors = tuple(result.errors) if result else ()
        settings = {key: self._settings.value(key) for key in self._settings.allKeys()}
        status_log = self._status.text()

        def failed(error: str) -> None:
            self._status.setText("Діагностику не створено; наявний файл не змінено.")
            QMessageBox.warning(self._parent, "Помилка діагностики", error)

        self._status.setText("Створюю приватний архів діагностики у фоні…")
        self._ui_bg_run(
            lambda: diagnostics.export_diagnostics_bundle(
                destination, logs=(status_log, self._read_crash_log()),
                errors=errors, settings=settings,
                scanned_paths=scanned, include_scanned_paths=include_paths),
            lambda value: self._status.setText(
                f"Діагностику збережено ({human(value.bytes_written)}): {destination}"),
            failed,
        )
