"""Профіль сканування й правила розумного вибору для головного вікна.

Співробітник Main: не знає нічого про вкладки чи сканування, лише про
мітку профілю, рядок статусу й функції зворотного виклику для читання й
запису поточного профілю та правил вибору, які отримує в конструкторі.
"""
from __future__ import annotations

from typing import Callable, Protocol

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox, QWidget

import dupscan.infra.preferences as preferences


class _PreferencesDialogLike(Protocol):
    saved_profile: "preferences.ScanProfile | None"

    def exec(self) -> int: ...


class SettingsController:
    """Діалог налаштувань (профіль сканування, правила розумного вибору)."""

    def __init__(
        self,
        parent: QWidget,
        get_status: Callable[[], QLabel],
        get_profile_summary: Callable[[], QLabel],
        settings: QSettings,
        get_profile: Callable[[], "preferences.ScanProfile"],
        set_profile: Callable[["preferences.ScanProfile"], None],
        get_profiles: Callable[[], "list[preferences.ScanProfile]"],
        set_profiles: Callable[["list[preferences.ScanProfile]"], None],
        set_selection_rules: Callable[["preferences.SelectionRules"], None],
        make_dialog: Callable[..., _PreferencesDialogLike],
    ) -> None:
        self._parent = parent
        self._get_status = get_status
        self._get_profile_summary = get_profile_summary
        self._settings = settings
        self._get_profile = get_profile
        self._set_profile = set_profile
        self._get_profiles = get_profiles
        self._set_profiles = set_profiles
        self._set_selection_rules = set_selection_rules
        self._make_dialog = make_dialog

    def refresh_profile_summary(self) -> None:
        profile_summary = self._get_profile_summary()
        name = self._get_profile().name
        profile_summary.setText(f"Профіль сканування: {name}")
        profile_summary.setAccessibleName(f"Активний профіль сканування: {name}")

    def show_preferences(self) -> None:
        try:
            profiles = preferences.list_profiles()
            rules = preferences.load_selection_rules()
        except preferences.PreferencesError as error:
            QMessageBox.warning(
                self._parent, "DupScan",
                "Не вдалося прочитати налаштування: " + str(error))
            return
        dialog = self._make_dialog(self._parent, profiles, self._get_profile(), rules)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.saved_profile is None:
            return
        self._set_profile(dialog.saved_profile)
        self._set_selection_rules(preferences.load_selection_rules())
        self._set_profiles(preferences.list_profiles())
        self.refresh_profile_summary()
        self._settings.setValue("scan/current_profile", self._get_profile().name)
        self._get_status().setText(
            f"Застосовано профіль «{self._get_profile().name}» і правила "
            "розумного вибору.")
