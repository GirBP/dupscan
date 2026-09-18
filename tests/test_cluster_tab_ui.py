"""Вкладка «Кластери тек» у GUI.

Не про алгоритм (те в tests/test_clusters.py) — про безпечне дротування:
self.tabs.currentIndex() індексує кортеж views у resizeEvent, тож 4-та
вкладка мусить бути в цьому кортежі, а не лише в переліку вкладок —
інакше IndexError. Такий збій ловиться лише при ресайзі вікна на новій
вкладці, а не при простому запуску, тому окремий тест виправданий.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, QSize  # noqa: E402
from PySide6.QtGui import QResizeEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make(path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _main_with_cluster(tmp_path):
    """Три теки, зв'язані транзитивно (A-B файлом X, B-C файлом Y) — один
    кластер із трьох тек."""
    _make(tmp_path / "A" / "x.bin", b"content-x")
    _make(tmp_path / "B" / "x.bin", b"content-x")
    _make(tmp_path / "B" / "y.bin", b"content-y")
    _make(tmp_path / "C" / "y.bin", b"content-y")
    result = core.scan([str(tmp_path)])
    main = app.Main()
    main.result = result
    main._finish_model_refresh(result)
    _qapp.processEvents()
    return main, result


def test_cluster_tab_populated_after_refresh(tmp_path):
    main, _result = _main_with_cluster(tmp_path)
    assert len(main.m_clusters.clusters) == 1
    cluster = main.m_clusters.clusters[0]
    assert cluster.count == 3
    assert "Кластери тек (1)" == main.tabs.tabText(3)


def test_switching_to_clusters_tab_and_resizing_does_not_crash(tmp_path):
    """Регресія проти IndexError: (v_files, v_dirs, v_sim)[currentIndex()]
    без явного branching за типом вкладки падає на індексі 3."""
    main, _result = _main_with_cluster(tmp_path)
    main.tabs.setCurrentIndex(3)
    _qapp.processEvents()
    resize = QResizeEvent(QSize(900, 700), QSize(800, 600))
    QApplication.sendEvent(main, resize)
    _qapp.processEvents()
    main._update_selection_summary()  # застереження: кластери не плутаються з подібністю


def test_clusters_tab_selection_disables_trash_actions(tmp_path):
    """На вкладці кластерів немає що позначати/видаляти — кнопки Кошика
    мають лишатись неактивними, а не тягнути позначки з подібності."""
    main, _result = _main_with_cluster(tmp_path)
    main.tabs.setCurrentIndex(3)
    idx = main.m_clusters.index(0, 0)
    main.v_clusters.setCurrentIndex(idx)
    main._update_selection_summary()
    main._update_action_state()
    assert not main.action_trash_current.isEnabled()
    assert not main.action_toggle_trash_mark.isEnabled()


def test_find_pair_for_cluster_dir_handles_no_match_gracefully(tmp_path):
    main, _result = _main_with_cluster(tmp_path)
    main._find_pair_for_cluster_dir(str(tmp_path / "A"))
    assert "Немає відповідної пари" in main.status.text()


def test_cluster_model_has_no_checked_state():
    """Структурний read-only ще й на рівні реального GUI-класу моделі."""
    model = app.ClusterModel()
    assert not hasattr(model, "checked")


def test_reveal_cluster_dir_calls_reveal_with_dir_path(tmp_path, monkeypatch):
    main, _result = _main_with_cluster(tmp_path)
    revealed = []
    monkeypatch.setattr(app, "reveal", lambda path: revealed.append(path))
    child = main.m_clusters.index(0, 0, main.m_clusters.index(0, 0))
    main._reveal_cluster_dir(child)
    dir_path = main.m_clusters.dir_at(child)
    assert dir_path is not None
    assert revealed == [dir_path]


def test_reveal_cluster_dir_ignores_header_row(tmp_path, monkeypatch):
    """Кореневий (кластерний) рядок не має теки — reveal() не кличеться."""
    main, _result = _main_with_cluster(tmp_path)
    revealed = []
    monkeypatch.setattr(app, "reveal", lambda path: revealed.append(path))
    main._reveal_cluster_dir(main.m_clusters.index(0, 0))
    assert revealed == []


class _FakeAction:
    def __init__(self, text):
        self.text_ = text
        self.enabled_ = True

    def setEnabled(self, value):  # noqa: N802 — Qt API
        self.enabled_ = value


class _CapturingMenu:
    """Підміна QMenu, що лише записує додані дії без модального .exec()."""

    def __init__(self, *_a, **_kw):
        self.actions_: list[_FakeAction] = []

    def addAction(self, text):  # noqa: N802 — Qt API
        action = _FakeAction(text)
        self.actions_.append(action)
        return action

    def exec(self, *_a, **_kw):  # noqa: N802 — Qt API
        return None


def _expanded_child_index(main):
    root = main.m_clusters.index(0, 0)
    main.v_clusters.expand(root)
    return main.m_clusters.index(0, 0, root)


def test_clusters_menu_has_reveal_and_find_pair_actions(tmp_path, monkeypatch):
    main, _result = _main_with_cluster(tmp_path)
    idx = _expanded_child_index(main)
    captured = {}
    monkeypatch.setattr(app, "QMenu",
                         lambda *a, **kw: captured.setdefault(
                             "menu", _CapturingMenu(*a, **kw)) or captured["menu"])
    main.v_clusters.setCurrentIndex(idx)
    main._clusters_menu(main.v_clusters.visualRect(idx).center())
    texts = {a.text_: a.enabled_ for a in captured["menu"].actions_}
    assert texts == {
        "Показати у Finder": True,
        "Знайти пару в Подібності": True,
    }


def test_clusters_menu_ignores_click_outside_any_row(tmp_path, monkeypatch):
    main, _result = _main_with_cluster(tmp_path)
    called = []
    monkeypatch.setattr(app, "QMenu", lambda *a, **kw: called.append(1))
    main._clusters_menu(QPoint(5000, 5000))
    assert called == []


def test_clusters_menu_reveal_action_calls_reveal(tmp_path, monkeypatch):
    main, _result = _main_with_cluster(tmp_path)
    idx = _expanded_child_index(main)
    revealed = []
    monkeypatch.setattr(app, "reveal", lambda path: revealed.append(path))

    class _ClickingMenu(_CapturingMenu):
        def exec(self, *_a, **_kw):  # noqa: N802 — Qt API
            for a in self.actions_:
                if a.text_ == "Показати у Finder":
                    return a
            return None

    monkeypatch.setattr(app, "QMenu", _ClickingMenu)
    main.v_clusters.setCurrentIndex(idx)
    main._clusters_menu(main.v_clusters.visualRect(idx).center())
    assert revealed == [main.m_clusters.dir_at(idx)]
