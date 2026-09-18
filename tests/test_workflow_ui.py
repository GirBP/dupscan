"""Task-oriented IA contracts for DupScan 2.1."""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QToolBar  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.ui.workflow_ui as workflow_ui  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def test_navigation_state_is_task_safe_and_scan_busy_is_a_barrier():
    state = workflow_ui.navigation_state(
        workflow_ui.TASK_RESULTS, has_result=False, scan_busy=False)
    assert state.active_task == workflow_ui.TASK_SCAN
    assert not state.results_enabled

    state = workflow_ui.navigation_state(
        workflow_ui.TASK_COMPARE, has_result=True, scan_busy=True)
    assert state.active_task == workflow_ui.TASK_SCAN
    assert not state.results_enabled
    assert state.scan_busy

    with pytest.raises(ValueError):
        workflow_ui.navigation_state("unknown", has_result=False, scan_busy=False)


def test_result_summary_and_progressive_action_bar_are_data_driven():
    result = core.ScanResult(
        file_groups=[core.FileGroup(100, "f", ["/a", "/b", "/c"])],
        dir_groups=[core.DirGroup(500, 2, ["/one", "/two"])],
        sim_pairs=[core.SimPair("/one", "/two", 50.0, 100)],
        files_seen=42,
        live=False,
    )
    summary = workflow_ui.summarize_result(result)
    assert summary.files_seen == 42
    assert summary.file_groups == 1
    assert summary.directory_groups == 1
    assert summary.similarity_pairs == 1
    assert summary.reclaim_bytes == 200
    assert summary.read_only
    assert not workflow_ui.selection_bar_visible(0)
    assert workflow_ui.selection_bar_visible(1)
    with pytest.raises(ValueError):
        workflow_ui.selection_bar_visible(-1)


def test_initial_shell_has_one_visible_command_per_scan_task():
    main = app.Main()
    main.show()
    _qapp.processEvents()

    assert main.task_stack.currentWidget() is main.page_scan
    assert main.nav_scan.isChecked()
    assert not main.nav_results.isEnabled()
    visible_buttons = [
        button.text() for button in main.findChildren(QPushButton)
        if button.isVisible()
    ]
    assert sum("Додати джерела" in text for text in visible_buttons) == 1
    assert sum("Почати сканування" in text for text in visible_buttons) == 1
    assert sum("Порівняти A / B" in text for text in visible_buttons) == 1
    assert all("Додати диск" not in text for text in visible_buttons)
    assert not hasattr(main, "b_sources")
    assert not hasattr(main, "profile_combo")
    assert "Профіль сканування:" in main.profile_summary.text()
    assert all(toolbar.isHidden() for toolbar in main.findChildren(QToolBar))
    assert main.operation_controls.isHidden()
    main.close()


def test_operation_controls_are_global_and_only_visible_while_busy():
    main = app.Main()
    main.show()
    _qapp.processEvents()

    main._set_operation_controls_visible(True)
    main.b_pause.setEnabled(True)
    main.b_cancel.setEnabled(True)
    _qapp.processEvents()

    assert not main.operation_controls.isHidden()
    assert main.operation_controls.parent() is not main.page_scan
    assert main.b_pause.isVisible()
    assert main.b_cancel.isVisible()

    main._set_operation_controls_visible(False)
    assert main.operation_controls.isHidden()
    # 2.16: місце банера зарезервоване завжди (layout не смикається),
    # тому «неактивний» стан — прозорий банер без тексту, не isHidden.
    assert main.results_operation_banner.property("active") == "false"
    assert main.results_operation_text.text() == ""
    main.close()


def test_results_warn_when_read_errors_can_hide_large_folder_pairs():
    result = core.ScanResult(
        errors=[
            "[Errno 70] файл змінився після обходу: '/Volumes/L/a'",
            "[Errno 2] No such file or directory: '/Volumes/L/b'",
        ],
        files_seen=10,
        live=False,
    )
    main = app.Main()
    main.result = result

    main._finish_model_refresh(result)

    assert not main.result_quality_notice.isHidden()
    text = main.result_quality_text.text()
    assert "2 шляхів не прочитано" in text
    assert "Великі схожі теки" in text
    assert "Файл змінився: 1" in text
    assert "Елемент зник: 1" in text
    main.close()


def test_persisted_sort_can_restore_during_shell_construction(
        tmp_path, monkeypatch):
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(settings_dir))
    settings = QSettings(str(settings_dir / "ui.ini"), QSettings.IniFormat)
    for name in ("files", "dirs", "similarity"):
        settings.setValue(f"views/{name}/sort_column", 1)
        settings.setValue(f"views/{name}/sort_order", 1)
    settings.sync()

    status_ready = []
    original = app.Main._capture_sort_state

    def capture(main, view):
        status_ready.append(hasattr(main, "status"))
        return original(main, view)

    monkeypatch.setattr(app.Main, "_capture_sort_state", capture)
    main = app.Main()

    assert status_ready
    assert all(status_ready)
    assert main.status.parent() is not None
    assert main.status.accessibleName()
    main.close()


def test_results_unlock_after_scan_and_trash_bar_is_contextual():
    result = core.ScanResult(
        file_groups=[core.FileGroup(100, "f", ["/a", "/b"])],
        files_seen=2,
        bytes_seen=200,
        live=True,
    )
    main = app.Main()
    main.on_done(result)
    _qapp.processEvents()

    assert main.task_stack.currentWidget() is main.page_results
    assert main.nav_results.isEnabled()
    assert main.nav_results.isChecked()
    assert main.selection_bar.isHidden()

    parent = main.m_files.index(0, 0)
    child = main.m_files.index(0, 0, parent)
    assert main.m_files.setData(child, Qt.Checked, Qt.CheckStateRole)
    _qapp.processEvents()
    assert not main.selection_bar.isHidden()
    assert "пакет (1)" in main.b_trash_marked.text().casefold()
    main.close()


def test_loaded_result_is_visibly_read_only_and_compare_roles_are_explicit():
    result = core.ScanResult(
        file_groups=[core.FileGroup(100, "f", ["/a", "/b"])],
        files_seen=2,
        live=False,
    )
    main = app.Main()
    main.result = result
    main.m_files.set_groups(result.file_groups)
    main._finish_model_refresh(result)
    main._show_task(workflow_ui.TASK_RESULTS)
    assert "ЛИШЕ ПЕРЕГЛЯД" in main.summary.text()
    assert not main.b_trash_marked.isEnabled()

    main._show_task(workflow_ui.TASK_COMPARE)
    labels = {label.text() for label in main.page_compare.findChildren(QLabel)}
    assert "Тека A" in labels
    assert "Тека B" in labels
    main.close()
