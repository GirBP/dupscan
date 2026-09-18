"""UI-стани паузи: числа у статусі, заморозка busy-бара, миттєвий рендер
після продовження. Offscreen-Qt, без реального скану (FakeWorker)."""

import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())  # ізоляція даних

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


class FakeWorker:
    def __init__(self):
        self.pause = threading.Event()
        self.cancel = threading.Event()

    def isRunning(self):
        return True


def make_main() -> app_mod.Main:
    m = app_mod.Main()
    m.worker = FakeWorker()
    return m


def test_pause_shows_counts_and_remaining():
    m = make_main()
    m.on_progress("Повне хешування", 10, 100)
    m.toggle_pause()
    t = m.status.text()
    assert "Призупинено" in t and "10 з 100" in t and "лишилось 90" in t
    assert m.bar.maximum() == 100  # визначений бар не чіпаємо


def test_pause_freezes_busy_bar():
    m = make_main()
    m.on_progress("Обхід тек", 5120, 0)
    assert m.bar.maximum() == 0  # busy-режим (анімується)
    m.toggle_pause()
    assert m.bar.maximum() == 1  # заморожено — анімації нема
    t = m.status.text()
    assert "Обхід тек" in t and "5120" in t
    m.toggle_pause()  # продовжити
    assert m.bar.maximum() == 0  # рендер зі збереженого стану (total==0)
    assert m.b_pause.text() == "Пауза"


def test_pause_before_first_tick():
    m = make_main()
    m.bar.setRange(0, 0)  # як після start_scan
    m.toggle_pause()
    assert "Призупинено" in m.status.text()
    assert m.bar.maximum() == 1
    m.toggle_pause()
    assert m.bar.maximum() == 0


def test_late_tick_updates_paused_numbers():
    m = make_main()
    m.on_progress("Повне хешування", 10, 100)
    m.toggle_pause()
    m.on_progress("Повне хешування", 12, 100)  # запізнілий тик під паузою
    t = m.status.text()
    assert "12 з 100" in t and "Призупинено" in t
    assert m.bar.maximum() == 100


def test_resume_renders_stored_state_instantly():
    m = make_main()
    m.on_progress("Повне хешування", 10, 100)
    m.toggle_pause()
    m.toggle_pause()
    assert m.status.text() == "Повне хешування: 10 / 100"
    assert m.bar.value() == 10 and m.bar.maximum() == 100


def test_cancel_from_pause_shows_status():
    m = make_main()
    m.on_progress("Повне хешування", 10, 100)
    m.toggle_pause()
    m.cancel_scan()
    assert m.worker.cancel.is_set()
    assert not m.worker.pause.is_set()
    assert "Скасовую" in m.status.text()
    assert m.b_pause.text() == "Пауза"
