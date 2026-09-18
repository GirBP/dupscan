"""Дротування вікна до журналу видалень: show_removal_history."""

import os
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QDialog, QListWidget, QMessageBox, QPushButton, QSplitter,
)

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.infra.removal_history as removal_history  # noqa: E402
from dupscan.format import when  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def wait_until(cond, timeout=8.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        _qapp.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _open_history(main, monkeypatch):
    monkeypatch.setattr(app_mod.QDialog, "exec", lambda _self: None)
    main.show_removal_history()
    dialogs = [d for d in main.findChildren(QDialog)
               if d.windowTitle() == "Видалення й відновлення — DupScan"]
    assert dialogs
    return dialogs[-1]


def _button(dlg, text: str) -> QPushButton:
    matches = [b for b in dlg.findChildren(QPushButton) if b.text() == text]
    assert matches, f"кнопка {text!r} мусить бути в діалозі"
    return matches[0]


def _lists(dlg):
    splitters = dlg.findChildren(QSplitter)
    assert len(splitters) == 1
    splitter = splitters[0]
    assert splitter.count() == 2
    operations_list, items_list = splitter.widget(0), splitter.widget(1)
    assert isinstance(operations_list, QListWidget)
    assert isinstance(items_list, QListWidget)
    return operations_list, items_list


def _operation(op_id="op-1", created_ns=1_700_000_000_000_000_000, status="done",
               items=None):
    return {
        "id": op_id,
        "created_ns": created_ns,
        "status": status,
        "items": items if items is not None else [],
    }


def _item(path="/scan/dup.bin", status="trashed", size=42,
          trashed_path="/Trash/dup.bin", identity="abc", kind="file", digest=None):
    return {
        "original_path": path, "status": status, "size": size,
        "trashed_path": trashed_path, "identity": identity,
        "kind": kind, "digest": digest,
    }


def test_dialog_lists_operations_and_selecting_one_shows_its_items(monkeypatch):
    main = app_mod.Main()
    item = _item()
    operation = _operation(items=[item])
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [operation])

    dlg = _open_history(main, monkeypatch)
    operations_list, items_list = _lists(dlg)
    assert wait_until(lambda: operations_list.count() == 1)
    assert operations_list.item(0).text() == (
        f"{when(operation['created_ns'])} · 1 елем. · done")
    assert operations_list.currentRow() == 0
    assert wait_until(lambda: items_list.count() == 1)
    assert items_list.item(0).text() == "trashed · 42 Б · /scan/dup.bin"
    main.deleteLater()


def test_dialog_shows_restored_count_in_operation_label(monkeypatch):
    main = app_mod.Main()
    items = [_item(path="/a"), dict(_item(path="/b"), status="restored")]
    operation = _operation(items=items)
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [operation])

    dlg = _open_history(main, monkeypatch)
    operations_list, _items_list = _lists(dlg)
    assert wait_until(lambda: operations_list.count() == 1)
    assert "відновлено 1" in operations_list.item(0).text()
    main.deleteLater()


def test_dialog_shows_error_when_history_cannot_be_read(monkeypatch):
    main = app_mod.Main()

    def broken(**_kw):
        raise OSError("журнал недоступний")

    monkeypatch.setattr(removal_history, "list_operations", broken)

    dlg = _open_history(main, monkeypatch)
    assert wait_until(
        lambda: "Не вдалося прочитати журнал" in _info_text(dlg))
    assert "журнал недоступний" in _info_text(dlg)
    main.deleteLater()


def _info_text(dlg) -> str:
    from PySide6.QtWidgets import QLabel
    labels = dlg.findChildren(QLabel)
    return labels[0].text() if labels else ""


def test_restore_without_scanned_roots_refuses_and_does_not_restore(monkeypatch):
    main = app_mod.Main()
    main._last_roots = ()
    item = _item()
    operation = _operation(items=[item])
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [operation])
    restore_calls = []
    monkeypatch.setattr(
        removal_history, "restore_item",
        lambda *a, **kw: restore_calls.append((a, kw)))
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: infos.append((title, text)))

    dlg = _open_history(main, monkeypatch)
    operations_list, items_list = _lists(dlg)
    assert wait_until(lambda: items_list.count() == 1)
    items_list.setCurrentRow(0)
    _button(dlg, "Відновити вибране").click()

    assert not restore_calls
    assert infos and "підтвердити безпечну область" in infos[-1][1]
    main.deleteLater()


def test_restore_without_automatic_proof_refuses_and_suggests_finder(monkeypatch):
    main = app_mod.Main()
    main._last_roots = ("/scan",)
    item = _item(identity=None, digest=None)
    operation = _operation(items=[item])
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [operation])
    monkeypatch.setattr(
        removal_history, "can_restore_automatically", lambda _item: False)
    restore_calls = []
    monkeypatch.setattr(
        removal_history, "restore_item",
        lambda *a, **kw: restore_calls.append((a, kw)))
    infos = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: infos.append((title, text)))

    dlg = _open_history(main, monkeypatch)
    operations_list, items_list = _lists(dlg)
    assert wait_until(lambda: items_list.count() == 1)
    items_list.setCurrentRow(0)
    _button(dlg, "Відновити вибране").click()

    assert not restore_calls
    assert infos and "відновлення через Finder" in infos[-1][1] or (
        infos and "Finder" in infos[-1][1])
    main.deleteLater()


def test_restore_success_calls_restore_item_and_reloads_operations(monkeypatch):
    main = app_mod.Main()
    main._last_roots = ("/scan",)
    item = _item()
    operation = _operation(items=[item])
    list_calls = []

    def fake_list(**kw):
        list_calls.append(kw)
        return [operation]

    monkeypatch.setattr(removal_history, "list_operations", fake_list)
    monkeypatch.setattr(
        removal_history, "can_restore_automatically", lambda _item: True)
    restore_calls = []

    def fake_restore(operation_id, original_path, *, allowed_roots):
        restore_calls.append((operation_id, original_path, allowed_roots))
        return {"status": "restored"}

    monkeypatch.setattr(removal_history, "restore_item", fake_restore)

    dlg = _open_history(main, monkeypatch)
    operations_list, items_list = _lists(dlg)
    assert wait_until(lambda: items_list.count() == 1)
    items_list.setCurrentRow(0)
    _button(dlg, "Відновити вибране").click()

    assert wait_until(lambda: len(restore_calls) == 1)
    assert restore_calls[0] == (
        operation["id"], item["original_path"], ("/scan",))
    assert wait_until(lambda: len(list_calls) >= 2)
    main.deleteLater()


def test_restore_failure_shows_warning(monkeypatch):
    main = app_mod.Main()
    main._last_roots = ("/scan",)
    item = _item()
    operation = _operation(items=[item])
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [operation])
    monkeypatch.setattr(
        removal_history, "can_restore_automatically", lambda _item: True)

    def fake_restore(*_a, **_kw):
        raise OSError("не вдалося перейменувати")

    monkeypatch.setattr(removal_history, "restore_item", fake_restore)
    warnings = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, title, text: warnings.append((title, text)))

    dlg = _open_history(main, monkeypatch)
    operations_list, items_list = _lists(dlg)
    assert wait_until(lambda: items_list.count() == 1)
    items_list.setCurrentRow(0)
    _button(dlg, "Відновити вибране").click()

    assert wait_until(lambda: bool(warnings))
    assert warnings[-1][0] == "Не вдалося відновити"
    assert "не вдалося перейменувати" in warnings[-1][1]
    main.deleteLater()


def test_trash_button_opens_finder_trash(monkeypatch):
    main = app_mod.Main()
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [])
    popen_calls = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda args: popen_calls.append(args))

    dlg = _open_history(main, monkeypatch)
    _button(dlg, "Відкрити Кошик").click()

    assert popen_calls == [["open", os.path.expanduser("~/.Trash")]]
    main.deleteLater()


def test_close_button_accepts_dialog(monkeypatch):
    main = app_mod.Main()
    monkeypatch.setattr(
        removal_history, "list_operations", lambda **_kw: [])

    dlg = _open_history(main, monkeypatch)
    _button(dlg, "Закрити").click()

    assert dlg.result() == QDialog.DialogCode.Accepted
    main.deleteLater()
