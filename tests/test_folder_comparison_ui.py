"""Дротування вікна до порівняння двох тек: compare_two_folders,
_show_folder_comparison."""

import dataclasses
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_compare_refuses_while_an_operation_is_busy(monkeypatch, tmp_path):
    main = app_mod.Main()
    main._loading = True
    pick_calls = []
    monkeypatch.setattr(
        app_mod, "pick_dirs", lambda *a, **kw: pick_calls.append((a, kw)) or [])
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: infos.append((title, text)))

    main.compare_two_folders()

    assert not pick_calls
    assert infos and infos[-1] == ("DupScan", "Зачекайте завершення операції.")
    main.deleteLater()


def test_compare_does_nothing_when_no_folders_picked(monkeypatch):
    main = app_mod.Main()
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *a, **kw: [])
    begin_calls = []
    monkeypatch.setattr(main, "_begin_scan", begin_calls.append)

    main.compare_two_folders()

    assert not begin_calls
    assert main._compare_after_scan is None
    main.deleteLater()


def test_compare_requires_exactly_two_distinct_folders(monkeypatch, tmp_path):
    main = app_mod.Main()
    only_one = str(tmp_path / "A")
    os.makedirs(only_one)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *a, **kw: [only_one])
    begin_calls = []
    monkeypatch.setattr(main, "_begin_scan", begin_calls.append)
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: infos.append((title, text)))

    main.compare_two_folders()

    assert not begin_calls
    assert main._compare_after_scan is None
    assert infos and infos[-1][0] == "Потрібно дві теки"
    main.deleteLater()


def test_compare_deduplicates_identical_folders_to_one_path(monkeypatch, tmp_path):
    main = app_mod.Main()
    folder = str(tmp_path / "A")
    os.makedirs(folder)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *a, **kw: [folder, folder])
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: infos.append((title, text)))
    begin_calls = []
    monkeypatch.setattr(main, "_begin_scan", begin_calls.append)

    main.compare_two_folders()

    assert not begin_calls
    assert infos and infos[-1][0] == "Потрібно дві теки"
    main.deleteLater()


def test_compare_with_two_folders_stages_comparison_adds_dirs_and_scans(
        monkeypatch, tmp_path):
    main = app_mod.Main()
    dir_a = str(tmp_path / "A")
    dir_b = str(tmp_path / "B")
    os.makedirs(dir_a)
    os.makedirs(dir_b)
    monkeypatch.setattr(app_mod, "pick_dirs", lambda *a, **kw: [dir_a, dir_b])
    begin_calls = []
    monkeypatch.setattr(main, "_begin_scan", begin_calls.append)

    main.compare_two_folders()

    assert main._compare_after_scan == (
        os.path.abspath(dir_a), os.path.abspath(dir_b))
    listed = [main.folders.item(i).text() for i in range(main.folders.count())]
    assert os.path.abspath(dir_a) in listed
    assert os.path.abspath(dir_b) in listed
    assert begin_calls == [[os.path.abspath(dir_a), os.path.abspath(dir_b)]]
    main.deleteLater()


def test_pick_dirs_receives_expected_dialog_title(monkeypatch):
    main = app_mod.Main()
    calls = []

    def fake_pick_dirs(parent, *, title, accept_label):
        calls.append((parent, title, accept_label))
        return []

    monkeypatch.setattr(app_mod, "pick_dirs", fake_pick_dirs)

    main.compare_two_folders()

    assert calls == [(main, "Обрати теки A і B", "Використати як A і B")]
    main.deleteLater()


def test_show_folder_comparison_does_nothing_without_a_scan_result():
    main = app_mod.Main()
    assert main.result is None

    main._show_folder_comparison("/a", "/b")

    dialogs = [d for d in main.findChildren(QDialog)
               if d.windowTitle() == "Порівняння двох тек — DupScan"]
    assert not dialogs
    main.deleteLater()


def test_show_folder_comparison_opens_dialog_with_computed_comparison(
        monkeypatch, tmp_path):
    main = app_mod.Main()
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    make(dir_a / "same.bin", b"same")
    make(dir_b / "same.bin", b"same")
    make(dir_a / "only-a.bin", b"only-a")
    main.result = core.scan([str(dir_a), str(dir_b)])

    monkeypatch.setattr(app_mod.QDialog, "exec", lambda _self: None)

    main._show_folder_comparison(str(dir_a), str(dir_b))

    dialogs = [d for d in main.findChildren(QDialog)
               if d.windowTitle() == "Порівняння двох тек — DupScan"]
    assert len(dialogs) == 1
    dialog = dialogs[0]
    assert str(dir_a) in dialog.summary.text() or os.path.abspath(str(dir_a)) in (
        dialog.summary.text())
    assert "Однакових: 1" in dialog.summary.text()
    assert "лише A/B: 1/0" in dialog.summary.text()
    main.deleteLater()


def test_show_folder_comparison_marks_merge_verification_required_when_partial(
        monkeypatch, tmp_path):
    main = app_mod.Main()
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    make(dir_a / "x.bin", b"x")
    make(dir_b / "x.bin", b"x")
    result = core.scan([str(dir_a), str(dir_b)])
    main.result = dataclasses.replace(result, partial=True)

    captured = {}
    real_init = app_mod.FolderCompareDialog.__init__

    def spy_init(self, parent, comparison, merge_callback=None,
                 merge_verification_required=False):
        captured["merge_verification_required"] = merge_verification_required
        captured["merge_callback"] = merge_callback
        return real_init(
            self, parent, comparison, merge_callback,
            merge_verification_required=merge_verification_required)

    monkeypatch.setattr(app_mod.FolderCompareDialog, "__init__", spy_init)
    monkeypatch.setattr(app_mod.QDialog, "exec", lambda _self: None)

    main._show_folder_comparison(str(dir_a), str(dir_b))

    assert captured["merge_verification_required"] is True
    assert captured["merge_callback"] == main._sim_merge
    main.deleteLater()
