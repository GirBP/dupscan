"""Характеризаційні тести для дрібних чистих методів Main без прямого
покриття: стан паузи, лексична перевірка еталона, кандидат шляху сесії,
вибір активної моделі/вʼю вкладки, обʼєднання позначеного між вкладками.
Фіксують чинну поведінку перед подальшим розбиттям сусідніх методів."""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.domain.core as core  # noqa: E402
import dupscan.ui.app as app_mod  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def test_paused_status_without_progress():
    m = app_mod.Main()
    try:
        m._progress = None
        assert m._paused_status() == (
            "⏸ Призупинено. «Продовжити» відновить сканування.")
    finally:
        m.close()
        m.deleteLater()


def test_paused_status_with_known_total():
    m = app_mod.Main()
    try:
        m._progress = ("хешування", 3, 10)
        assert m._paused_status() == (
            "⏸ Призупинено — хешування: 3 з 10 (лишилось 7). "
            "«Продовжити» відновить.")
    finally:
        m.close()
        m.deleteLater()


def test_paused_status_without_known_total():
    m = app_mod.Main()
    try:
        m._progress = ("сканування", 5, 0)
        assert m._paused_status() == (
            "⏸ Призупинено — сканування: 5. «Продовжити» відновить.")
    finally:
        m.close()
        m.deleteLater()


def test_reference_blocked_true_when_cache_unavailable():
    m = app_mod.Main()
    try:
        m._reference_roots_cache = None
        assert m._reference_blocked("/будь-який/шлях") is True
    finally:
        m.close()
        m.deleteLater()


def test_reference_blocked_false_without_roots():
    m = app_mod.Main()
    try:
        m._reference_roots_cache = ()
        assert m._reference_blocked("/будь-який/шлях") is False
    finally:
        m.close()
        m.deleteLater()


def test_reference_blocked_true_under_protected_root(tmp_path):
    root = tmp_path / "еталон"
    root.mkdir()
    inside = root / "файл.txt"
    inside.write_text("x")
    m = app_mod.Main()
    try:
        m._reference_roots_cache = (str(root),)
        assert m._reference_blocked(str(inside)) is True
        assert m._reference_blocked(str(tmp_path / "поза" / "f.txt")) is False
    finally:
        m.close()
        m.deleteLater()


def test_session_path_candidate_without_result_normalizes():
    m = app_mod.Main()
    try:
        m.result = None
        candidate = m._session_path_candidate("./a/../b")
        assert candidate == os.path.normpath(os.path.abspath("./a/../b"))
    finally:
        m.close()
        m.deleteLater()


def test_session_path_candidate_live_result_normalizes_without_relink():
    m = app_mod.Main()
    try:
        m.result = core.ScanResult(live=True)
        candidate = m._session_path_candidate("./a/../b")
        assert candidate == os.path.normpath(os.path.abspath("./a/../b"))
    finally:
        m.close()
        m.deleteLater()


def test_active_model_view_maps_current_tab():
    m = app_mod.Main()
    try:
        m.tabs.setCurrentIndex(0)
        model, view = m._active_model_view()
        assert (model, view) == (m.m_files, m.v_files)

        m.tabs.setCurrentIndex(2)
        model, view = m._active_model_view()
        assert (model, view) == (m.m_sim, m.v_sim)
    finally:
        m.close()
        m.deleteLater()


def test_active_model_view_none_outside_checkable_tabs():
    m = app_mod.Main()
    try:
        m.tabs.setCurrentIndex(3)  # «Кластери тек» — не checkable-модель
        assert m._active_model_view() == (None, None)
    finally:
        m.close()
        m.deleteLater()


def test_selection_paths_unions_all_three_models():
    m = app_mod.Main()
    try:
        m.m_files.checked = {"/a/1"}
        m.m_dirs.checked = {"/b/1"}
        m.m_sim.checked = {"/c/1"}
        assert m._selection_paths() == {"/a/1", "/b/1", "/c/1"}
    finally:
        m.close()
        m.deleteLater()
