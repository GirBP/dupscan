"""F4: інтерфейс не стрибає.

Довжина тексту статусу і банера не має впливати на геометрію. Довгі шляхи
скорочуються посередині. Частота оновлень прогресу обмежена в одному місці.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402

_qapp = QApplication.instance() or QApplication([])

SHORT = "/Volumes/L/a.bin"
LONG = "/Volumes/L/" + "/".join(f"дуже-довга-тека-{i}" for i in range(30)) + "/f.bin"


def main_window():
    m = app_mod.Main()
    m.resize(1100, 700)
    m.show()
    _qapp.processEvents()
    return m


def test_status_geometry_is_text_independent():
    m = main_window()
    try:
        m.status.setText(f"Знімок теки · {SHORT} · МіБ: 1")
        _qapp.processEvents()
        short_hint = m.status.sizeHint().height()
        short_h = m.status.height()
        m.status.setText(f"Знімок теки · {LONG} · МіБ: 18837")
        _qapp.processEvents()
        assert m.status.sizeHint().height() == short_hint, (
            "висота статусу не має залежати від довжини тексту"
        )
        assert m.status.height() == short_h
    finally:
        m.close()


def test_banner_geometry_is_text_independent():
    m = main_window()
    try:
        m._set_results_operation(f"Знімок теки · {SHORT} · МіБ: 1", visible=True)
        _qapp.processEvents()
        hint = m.results_operation_text.sizeHint().height()
        m._set_results_operation(f"Знімок теки · {LONG} · МіБ: 18837", visible=True)
        _qapp.processEvents()
        assert m.results_operation_text.sizeHint().height() == hint, (
            "висота банера не має залежати від довжини тексту"
        )
    finally:
        m.close()


def test_long_path_is_elided_not_wrapped():
    m = main_window()
    try:
        m.status.setText(f"Знімок теки · {LONG} · МіБ: 18837")
        _qapp.processEvents()
        assert m.status.wordWrap() is False, "перенос слів робить стрибки висоти"
        shown = m.status.displayedText()
        assert "…" in shown or len(shown) < len(LONG), "довгий шлях мусить бути скорочений"
    finally:
        m.close()


def test_banner_space_is_reserved_when_hidden():
    m = main_window()
    try:
        m._set_results_operation("", visible=False)
        _qapp.processEvents()
        hidden = m.results_operation_banner.height()
        m._set_results_operation("Операція триває", visible=True)
        _qapp.processEvents()
        shown = m.results_operation_banner.height()
        assert hidden == shown, f"місце банера мусить бути зарезервоване: {hidden} проти {shown}"
    finally:
        m.close()


def test_progress_rate_is_capped_even_when_path_changes():
    """Зміна шляху не має обходити гейт частоти."""
    import dupscan.domain.core as core

    emitted: list[tuple[str, int, int]] = []
    limiter = app_mod.ProgressLimiter(lambda p, d, t: emitted.append((p, d, t)))
    for i in range(1000):
        limiter(f"Переношу · /том/тека/файл-{i}.bin", i, 1000)
    assert len(emitted) <= 12, f"на 1000 файлів мусить бути ≤12 оновлень, а було {len(emitted)}"
    assert core.PARTIAL > 0  # модуль справді завантажений
