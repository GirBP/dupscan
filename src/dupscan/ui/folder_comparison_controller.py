"""Порівняння двох тек для головного вікна.

Співробітник Main: не знає нічого про вкладки чи сканування, лише про
батьківський віджет для діалогів і функції зворотного виклику (вибір
тек, поточний результат скану, список джерел, запуск скану, злиття
пари подібності), які отримує в конструкторі.
"""
from __future__ import annotations

import os
from typing import Callable, Protocol

from PySide6.QtWidgets import QMessageBox, QWidget

import dupscan.domain.core as core
import dupscan.domain.product as product


class _DialogLike(Protocol):
    def exec(self) -> int: ...


class FolderComparisonController:
    """Обрати дві теки, просканувати їх і показати порівняння."""

    def __init__(
        self,
        parent: QWidget,
        pick_dirs: Callable[..., list[str]],
        get_operation_busy: Callable[[], bool],
        get_result: Callable[[], "core.ScanResult | None"],
        add_dir: Callable[[str], None],
        set_compare_after_scan: Callable[["tuple[str, str] | None"], None],
        begin_scan: Callable[[list[str]], None],
        get_sim_merge: Callable[[], Callable[..., None]],
        make_dialog: Callable[..., _DialogLike],
    ) -> None:
        self._parent = parent
        self._pick_dirs = pick_dirs
        self._get_operation_busy = get_operation_busy
        self._get_result = get_result
        self._add_dir = add_dir
        self._set_compare_after_scan = set_compare_after_scan
        self._begin_scan = begin_scan
        self._get_sim_merge = get_sim_merge
        self._make_dialog = make_dialog

    def compare_two_folders(self) -> None:
        if self._get_operation_busy():
            QMessageBox.information(
                self._parent, "DupScan", "Зачекайте завершення операції.")
            return
        paths = list(dict.fromkeys(
            os.path.abspath(path) for path in self._pick_dirs(
                self._parent,
                title="Обрати теки A і B",
                accept_label="Використати як A і B",
            )
        ))
        if not paths:
            return
        if len(paths) != 2:
            QMessageBox.information(
                self._parent, "Потрібно дві теки",
                "Оберіть рівно дві теки (⌘-клік у діалозі), щоб порівняти їх.")
            return
        self._set_compare_after_scan((paths[0], paths[1]))
        for path in paths:
            self._add_dir(path)
        self._begin_scan(paths)

    def show_folder_comparison(self, dir_a: str, dir_b: str) -> None:
        result = self._get_result()
        if result is None:
            return
        comparison = product.compare_folders(result, dir_a, dir_b)
        self._make_dialog(
            self._parent, comparison,
            self._get_sim_merge(),
            merge_verification_required=(not result.live or result.partial),
        ).exec()
