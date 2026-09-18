"""НАЙВАЖЛИВІШИЙ інваріант продукту:

перцептивна група — ПІДКАЗКА, не доказ (немає точного контентного
дайджеста), тож вона СТРУКТУРНО не може потрапити в деструктив. По
одному тесту на кожну деструктивну точку входу — жодна не приймає
PerceptualModel/PerceptualGroup мовчки; кожна відмовляє явно.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.ui.perceptual as perceptual  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _main() -> app.Main:
    main = app.Main()
    main.result = core.scan([])  # порожній, live-скан — досить для гейтів
    return main


def test_delete_checked_refuses_perceptual_model():
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main.delete_checked(main.m_perceptual)


def test_delete_checked_refuses_cluster_model():
    """Той самий гейт ловить і кластери тек — не лише фото."""
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main.delete_checked(main.m_clusters)


def test_delete_duplicate_paths_refuses_perceptual_model():
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main._delete_duplicate_paths(main.m_perceptual, {"/x"})


def test_verify_snapshot_group_then_trash_refuses_perceptual_model():
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main._verify_snapshot_group_then_trash(main.m_perceptual, {"/x"})


def test_trash_current_duplicate_refuses_when_active_model_is_perceptual(
        monkeypatch):
    """_active_model_view() навмисно НЕ включає PerceptualModel — цей тест
    імітує, наче хтось помилково приєднав його (майбутній рефакторинг), і
    підтверджує, що явна відмова спрацьовує, а не мовчазне "not
    GroupModel -> мабуть SimModel"."""
    main = _main()
    # check_index має бути ВАЛІДНИМ, інакше рання відмова "Оберіть синім
    # конкретний дублікат" спрацює раніше, ніж дійде до isinstance-гілки.
    monkeypatch.setattr(
        main, "_current_check_index",
        lambda: (main.m_perceptual, _AlwaysValidIndex(), "/some/path"))
    with pytest.raises(TypeError, match="GroupModel/SimModel"):
        main._trash_current_duplicate()


class _AlwaysValidIndex:
    """Мінімальний дублер QModelIndex.isValid()==True для тесту вище —
    справжній валідний QModelIndex вимагав би реальних рядків моделі."""

    def isValid(self) -> bool:  # noqa: N802 — Qt API
        return True


def test_perceptual_model_has_no_checked_state():
    """Структурний read-only ще й на рівні реального GUI-класу моделі —
    жодного поля, яке гейт Кошика міг би прийняти за позначку."""
    model = app.PerceptualModel()
    assert not hasattr(model, "checked")


def test_perceptual_group_is_not_a_file_group():
    """PerceptualGroup НЕ FileGroup — різні типи, ніякого спільного
    предка, що дозволив би деструктивному коду сплутати їх."""
    assert not isinstance(
        perceptual.PerceptualGroup(files=["/a"]), core.FileGroup)
    assert not issubclass(perceptual.PerceptualGroup, core.FileGroup)


def test_perceptual_context_menu_has_no_destructive_actions(tmp_path):
    """ПКМ на рядку перцептивної підказки: лише Quick Look/Показати у
    Finder — жодного «Кошик»/«Видалити»."""
    main = _main()
    main.m_perceptual.set_groups(
        [perceptual.PerceptualGroup(files=[str(tmp_path / "a.jpg"),
                                            str(tmp_path / "b.jpg")])])
    child = main.m_perceptual.index(
        0, 0, main.m_perceptual.index(0, 0))
    main.v_perceptual.setCurrentIndex(child)
    # _perceptual_menu будує QMenu і виконує modal .exec() — замінимо його
    # на фіктивний, що просто повертає перелік доданих дій без показу.
    captured = {}

    class _FakeMenu:
        def __init__(self, *_a, **_kw):
            self.actions_ = []

        def addAction(self, text):  # noqa: N802 — Qt API
            action = _FakeAction(text)
            self.actions_.append(action)
            return action

        def exec(self, *_a, **_kw):  # noqa: N802 — Qt API
            captured["texts"] = [a.text_ for a in self.actions_]
            return None

    class _FakeAction:
        def __init__(self, text):
            self.text_ = text

        def setEnabled(self, *_a):  # noqa: N802 — Qt API
            pass

    import dupscan.ui.app as app_module
    monkeypatch_target = app_module.QMenu
    app_module.QMenu = _FakeMenu
    try:
        main._perceptual_menu(main.v_perceptual.visualRect(child).center())
    finally:
        app_module.QMenu = monkeypatch_target
    texts = captured.get("texts", [])
    assert texts, "меню не побудувалось"
    forbidden = {"кошик", "видал", "trash", "delete"}
    for text in texts:
        low = text.lower()
        assert not any(word in low for word in forbidden), (
            f"деструктивна дія на перцептивному рядку: {text!r}")
    assert "Quick Look" in texts
    assert "Показати у Finder" in texts


def test_historical_session_shows_empty_perceptual_tab_with_explanation():
    """Historical сесія: вкладка порожня з чесним поясненням, не мотлох
    від попереднього live-скану."""
    main = _main()
    main.m_perceptual.set_groups(
        [perceptual.PerceptualGroup(files=["/old/a.jpg", "/old/b.jpg"])])
    historical = core.scan([])
    historical.live = False
    main.result = historical
    main._finish_model_refresh(historical)
    assert main.m_perceptual.groups == []
    assert not main.b_perceptual_scan.isEnabled()
    assert "не зберігаються" in main.l_perceptual_status.text()
