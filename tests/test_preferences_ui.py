"""Дротування вікна до діалогу налаштувань: show_preferences, профіль-мітка."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.infra.preferences as preferences  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


class _FakeDialog:
    """Замінник PreferencesDialog: фіксує аргументи конструктора, не показує GUI."""

    instances = []

    def __init__(self, parent, profiles, current_profile, rules):
        self.parent = parent
        self.profiles = profiles
        self.current_profile = current_profile
        self.rules = rules
        self.exec_result = QDialog.DialogCode.Rejected
        self.saved_profile = None
        _FakeDialog.instances.append(self)

    def exec(self):
        return self.exec_result


def test_refresh_profile_summary_sets_label_text_and_accessible_name():
    main = app_mod.Main()
    main._profile = preferences.ScanProfile(name="Great videos")
    main._refresh_profile_summary()
    assert main.profile_summary.text() == "Профіль сканування: Great videos"
    assert main.profile_summary.accessibleName() == (
        "Активний профіль сканування: Great videos")
    main.deleteLater()


def test_show_preferences_warns_and_does_not_open_dialog_on_read_error(monkeypatch):
    main = app_mod.Main()
    original_profile = main._profile
    original_rules = main._selection_rules
    original_profiles = main._profiles

    def broken(*_args, **_kwargs):
        raise preferences.PreferencesError("сховище пошкоджено")

    monkeypatch.setattr(app_mod.preferences, "list_profiles", broken)
    _FakeDialog.instances.clear()
    monkeypatch.setattr(app_mod, "PreferencesDialog", _FakeDialog)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, text: warnings.append((title, text)))

    main.show_preferences()

    assert not _FakeDialog.instances
    assert warnings and warnings[0][0] == "DupScan"
    assert "Не вдалося прочитати налаштування: сховище пошкоджено" in warnings[0][1]
    assert main._profile is original_profile
    assert main._selection_rules is original_rules
    assert main._profiles is original_profiles
    main.deleteLater()


def test_show_preferences_cancelled_dialog_changes_nothing(monkeypatch):
    main = app_mod.Main()
    original_profile = main._profile
    original_status = main.status.text()
    _FakeDialog.instances.clear()
    monkeypatch.setattr(app_mod, "PreferencesDialog", _FakeDialog)

    main.show_preferences()

    assert len(_FakeDialog.instances) == 1
    dialog = _FakeDialog.instances[0]
    assert dialog.exec_result == QDialog.DialogCode.Rejected
    assert main._profile is original_profile
    assert main.status.text() == original_status
    main.deleteLater()


def test_show_preferences_accepted_without_saved_profile_changes_nothing(monkeypatch):
    main = app_mod.Main()
    original_profile = main._profile
    original_status = main.status.text()
    _FakeDialog.instances.clear()
    monkeypatch.setattr(app_mod, "PreferencesDialog", _FakeDialog)

    def make_dialog(parent, profiles, current_profile, rules):
        dialog = _FakeDialog(parent, profiles, current_profile, rules)
        dialog.exec_result = QDialog.DialogCode.Accepted
        dialog.saved_profile = None
        return dialog

    monkeypatch.setattr(app_mod, "PreferencesDialog", make_dialog)

    main.show_preferences()

    assert main._profile is original_profile
    assert main.status.text() == original_status
    main.deleteLater()


def test_show_preferences_applies_saved_profile_and_persists_it(monkeypatch):
    main = app_mod.Main()
    new_profile = preferences.ScanProfile(name="Great videos", min_size=5_000)
    new_rules = preferences.SelectionRules(keep="oldest")

    monkeypatch.setattr(
        app_mod.preferences, "load_selection_rules", lambda: new_rules)
    monkeypatch.setattr(
        app_mod.preferences, "list_profiles",
        lambda: [preferences.DEFAULT_PROFILE, new_profile])

    def make_dialog(parent, profiles, current_profile, rules):
        dialog = _FakeDialog(parent, profiles, current_profile, rules)
        dialog.exec_result = QDialog.DialogCode.Accepted
        dialog.saved_profile = new_profile
        return dialog

    monkeypatch.setattr(app_mod, "PreferencesDialog", make_dialog)

    main.show_preferences()

    assert main._profile is new_profile
    assert main._selection_rules is new_rules
    assert main._profiles == [preferences.DEFAULT_PROFILE, new_profile]
    assert main.profile_summary.text() == "Профіль сканування: Great videos"
    assert main._settings.value("scan/current_profile") == "Great videos"
    assert main.status.text() == (
        "Застосовано профіль «Great videos» і правила розумного вибору.")
    main.deleteLater()
