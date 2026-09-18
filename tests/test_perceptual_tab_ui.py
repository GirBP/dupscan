"""Вкладка «Схожі фото (підказка)» — ДРОТУВАННЯ
(не деструктив-інваріант — те в test_perceptual_readonly_invariant.py; не
алгоритм — те в test_perceptual.py).
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QImage, QResizeEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make_image(path: str, seed: int, size: int = 48) -> None:
    img = QImage(size, size, QImage.Format.Format_RGB32)
    for y in range(size):
        for x in range(size):
            v = (x * 4 + y * 4 + seed) % 256
            img.setPixelColor(x, y, QColor(v, v, v))
    img.save(path, "PNG")


def _main_with_result(tmp_path):
    (tmp_path / "unrelated.bin").write_bytes(b"not-an-image")
    result = core.scan([str(tmp_path)])
    main = app.Main()
    main.result = result
    main._finish_model_refresh(result)
    _qapp.processEvents()
    return main, result


def test_perceptual_tab_index_is_four_and_starts_empty(tmp_path):
    main, _result = _main_with_result(tmp_path)
    assert main.tabs.tabText(4).startswith("Схожі фото")
    assert main.m_perceptual.groups == []
    assert main.b_perceptual_scan.isEnabled()


def test_switching_to_perceptual_tab_and_resizing_does_not_crash(tmp_path):
    """Регресія проти IndexError: та сама небезпека, що й з вкладкою
    кластерів — 5 елементів у кортежах tabs.currentIndex()."""
    main, _result = _main_with_result(tmp_path)
    main.tabs.setCurrentIndex(4)
    _qapp.processEvents()
    resize = QResizeEvent(QSize(900, 700), QSize(800, 600))
    QApplication.sendEvent(main, resize)
    _qapp.processEvents()
    main._update_selection_summary()


def test_scan_button_disabled_without_result(monkeypatch):
    main = app.Main()
    # QMessageBox.information() — модальний .exec(); без підміни завис би
    # назавжди в headless-тесті (нема кому клікнути OK).
    monkeypatch.setattr(app.QMessageBox, "information", lambda *a, **kw: None)
    main._perceptual_scan()  # без self.result — інформаційне вікно, не крах
    assert main.perceptual_worker is None


def test_perceptual_scan_end_to_end_populates_model_and_tab_text(tmp_path):
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    _make_image(str(a_dir / "photo.png"), seed=0)
    img = QImage(str(a_dir / "photo.png"))
    img.scaled(24, 24).save(str(b_dir / "photo_small.jpg"), "JPEG")

    main, _result = _main_with_result(tmp_path)
    main.result.scanned_roots = [str(tmp_path)]
    main._perceptual_scan()
    assert main.perceptual_worker is not None
    assert not main.b_perceptual_scan.isEnabled()
    # isHidden() — явний стан setVisible() САМОГО віджета, незалежно від
    # батьків (вікно ніколи не .show()-иться в headless-тестах, як і в
    # решті пакета; isVisible() тут завжди False незалежно від коду).
    assert not main.b_perceptual_cancel.isHidden()

    ok = main.perceptual_worker.wait(10_000)
    assert ok, "воркер не завершився вчасно"
    _qapp.processEvents()  # доставити queued done-сигнал у GUI-потік

    assert main.perceptual_worker is None
    assert main.b_perceptual_scan.isEnabled()
    assert len(main.m_perceptual.groups) == 1
    assert main.m_perceptual.groups[0].count == 2
    assert "Схожі фото (підказка) (1)" == main.tabs.tabText(4)


def test_perceptual_scan_refuses_for_historical_session(tmp_path, monkeypatch):
    main, result = _main_with_result(tmp_path)
    result.live = False
    started = {"flag": False}

    def fake_information(*_a, **_kw):
        started["flag"] = True

    monkeypatch.setattr(app.QMessageBox, "information", fake_information)
    main._perceptual_scan()
    assert started["flag"]
    assert main.perceptual_worker is None


def test_perceptual_cancel_sets_worker_cancel_event(tmp_path):
    for i in range(20):
        sub = tmp_path / f"d{i}"
        sub.mkdir()
        _make_image(str(sub / "p.png"), seed=i)
    main, _result = _main_with_result(tmp_path)
    main.result.scanned_roots = [str(tmp_path)]
    main._perceptual_scan()
    worker = main.perceptual_worker
    assert worker is not None
    main._perceptual_cancel()
    assert worker.cancel.is_set()
    worker.wait(10_000)
    _qapp.processEvents()


def test_perceptual_progress_shows_phase_and_counts(tmp_path):
    main, _result = _main_with_result(tmp_path)
    main._perceptual_progress("Хешую", 3, 10)
    assert main.l_perceptual_status.text() == "Хешую: 3/10"


def test_perceptual_progress_shows_bare_phase_without_total(tmp_path):
    main, _result = _main_with_result(tmp_path)
    main._perceptual_progress("Шукаю зображення…", 0, 0)
    assert main.l_perceptual_status.text() == "Шукаю зображення…"


class _FakeEvent:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def is_set(self):
        return self._set


class _FakeWorker:
    def __init__(self):
        self.pause = _FakeEvent()
        self.cancel = _FakeEvent()


def test_perceptual_pause_toggle_without_worker_is_noop(tmp_path):
    main, _result = _main_with_result(tmp_path)
    main._perceptual_pause_toggle(True)
    assert main.b_perceptual_pause.text() == "Пауза"


def test_perceptual_pause_toggle_on_and_off_with_worker(tmp_path):
    main, _result = _main_with_result(tmp_path)
    main.perceptual_worker = _FakeWorker()
    main._perceptual_pause_toggle(True)
    assert main.perceptual_worker.pause.is_set()
    assert main.b_perceptual_pause.text() == "Продовжити"
    assert main.l_perceptual_status.text() == "На паузі…"
    main._perceptual_pause_toggle(False)
    assert not main.perceptual_worker.pause.is_set()
    assert main.b_perceptual_pause.text() == "Пауза"


def test_perceptual_reset_controls_restores_idle_state(tmp_path):
    main, _result = _main_with_result(tmp_path)
    main.b_perceptual_scan.setEnabled(False)
    main.b_perceptual_pause.setVisible(True)
    main.b_perceptual_pause.setChecked(True)
    main.b_perceptual_pause.setText("Продовжити")
    main.b_perceptual_cancel.setVisible(True)
    main._perceptual_reset_controls()
    assert main.b_perceptual_scan.isEnabled()
    assert main.b_perceptual_pause.isHidden()
    assert not main.b_perceptual_pause.isChecked()
    assert main.b_perceptual_pause.text() == "Пауза"
    assert main.b_perceptual_cancel.isHidden()


def test_perceptual_failed_resets_worker_and_shows_message(tmp_path):
    main, _result = _main_with_result(tmp_path)
    main.perceptual_worker = _FakeWorker()
    main._perceptual_failed("диск відмовив")
    assert main.perceptual_worker is None
    assert "диск відмовив" in main.l_perceptual_status.text()


def test_perceptual_quick_look_calls_quick_look_for_valid_path(tmp_path, monkeypatch):
    main, _result = _main_with_result(tmp_path)
    main.m_perceptual.set_groups([
        app.perceptual.PerceptualGroup(files=[str(tmp_path / "a.jpg"),
                                               str(tmp_path / "b.jpg")])])
    child = main.m_perceptual.index(0, 0, main.m_perceptual.index(0, 0))
    looked = []
    monkeypatch.setattr(app, "quick_look", lambda path: looked.append(path))
    main._perceptual_quick_look(child)
    assert looked == [main.m_perceptual.path_at(child)]


def test_perceptual_quick_look_ignores_invalid_index(tmp_path, monkeypatch):
    main, _result = _main_with_result(tmp_path)
    looked = []
    monkeypatch.setattr(app, "quick_look", lambda path: looked.append(path))
    main._perceptual_quick_look(main.m_perceptual.index(-1, -1))
    assert looked == []
