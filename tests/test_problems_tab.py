"""Окрема вкладка «Проблеми» (index 5).

Read-only поверхня над тими самими помилками, що вже показує
ProblemsDialog/product.classify_problem — не про алгоритм класифікації
(те в tests/test_product.py), а про: (1) чисту модель ProblemsModel
(групування/порядок/обрізка) і (2) безпечне дротування 6-ї вкладки —
структурний read-only інваріант, як у ClusterModel/PerceptualModel, і
регресія проти IndexError у кортежах, які мають враховувати кожну
вкладку (порт кластерів/фото вже наступав на це).
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QResizeEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.ui.table_models as models  # noqa: E402
import dupscan.domain.product as product  # noqa: E402
import dupscan.ui.workflow_ui as workflow_ui  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _main() -> app.Main:
    main = app.Main()
    main.result = core.scan([])  # порожній, live-скан — досить для гейтів
    return main


def _result_with_errors(errors: list[str], total: int | None = None) -> core.ScanResult:
    result = core.ScanResult()
    result.errors = list(errors)
    result.errors_total = len(errors) if total is None else total
    return result


def _main_with_errors(errors: list[str], total: int | None = None):
    result = _result_with_errors(errors, total)
    main = app.Main()
    main.result = result
    main._finish_model_refresh(result)
    _qapp.processEvents()
    return main, result


# ---- Блок 1: ProblemsModel як чиста одиниця (table_models.py) --------------

def test_set_errors_groups_by_category_counts_and_advice():
    model = models.ProblemsModel()
    errors = [
        "not found: /a",
        "no such file: /b",
        "not found: /c",
        "Permission denied: /d",
        "permission denied: /e",
        "input/output error: /f",
    ]
    model.set_errors(errors, len(errors))
    assert [c.title for c in model.categories] == [
        "Елемент зник", "Немає доступу", "Помилка носія"]
    assert [c.count for c in model.categories] == [3, 2, 1]
    # порада йде напряму з product.classify_problem — та сама функція,
    # що вже використовує ProblemsDialog.
    expected_title, expected_advice = product.classify_problem("not found: /a")
    assert model.categories[0].title == expected_title
    assert model.categories[0].advice == expected_advice
    assert model.categories[0].messages == [
        "not found: /a", "no such file: /b", "not found: /c"]


def test_set_errors_breaks_count_ties_alphabetically_by_title():
    model = models.ProblemsModel()
    errors = [
        "input/output error: /a", "EIO: /b",          # Помилка носія x2
        "Permission denied: /c", "дозволу немає: /d",  # Немає доступу x2
    ]
    model.set_errors(errors, len(errors))
    assert [c.title for c in model.categories] == [
        "Немає доступу", "Помилка носія"]
    assert [c.count for c in model.categories] == [2, 2]


def test_set_errors_is_idempotent_and_rebuilds_from_scratch():
    model = models.ProblemsModel()
    model.set_errors(["not found: /a"], 1)
    assert len(model.categories) == 1
    model.set_errors([], 0)
    assert model.categories == []
    assert model.rowCount() == 0


def test_set_errors_tree_structure_root_and_children():
    model = models.ProblemsModel()
    model.set_errors(["not found: /a", "not found: /b"], 2)
    assert model.rowCount() == 1  # одна категорія
    root = model.index(0, 0)
    assert model.rowCount(root) == 2  # два повідомлення під нею
    assert model.data(root, 0) == "Елемент зник"
    assert model.data(model.index(0, 1), 0) == "2"  # лічильник
    child0 = model.index(0, 0, root)
    child1 = model.index(1, 0, root)
    assert model.data(child0) == "not found: /a"
    assert model.data(child1) == "not found: /b"
    assert model.message_at(child0) == "not found: /a"
    assert model.message_at(root) is None  # категорія — не повідомлення
    assert model.parent(child0) == root


def test_set_errors_shows_truncated_caption_when_total_exceeds_shown():
    model = models.ProblemsModel()
    errors = ["not found: /a", "not found: /b"]
    model.set_errors(errors, 500)
    assert model.rowCount() == len(model.categories) + 1
    caption_row = model.index(len(model.categories), 0)
    caption = model.data(caption_row)
    assert caption is not None
    assert "2" in caption and "500" in caption
    assert model.rowCount(caption_row) == 0  # підпис-рядок без дітей


def test_set_errors_no_caption_when_total_equals_shown():
    model = models.ProblemsModel()
    model.set_errors(["not found: /a"], 1)
    assert model.rowCount() == len(model.categories)


def test_problems_model_has_no_checked_state():
    """Структурний read-only ще й на рівні реального GUI-класу моделі —
    жодного поля, яке гейт Кошика міг би прийняти за позначку."""
    model = app.ProblemsModel()
    assert not hasattr(model, "checked")
    assert not hasattr(model, "set_tag")


# ---- Блок 2: вкладка існує, індекс 5, лічильник, порожній стан -------------

def test_problems_tab_exists_at_index_five_with_name():
    main = _main()
    assert main.tabs.count() == 6
    assert main.tabs.tabText(5) == "Проблеми"
    assert main.v_problems.model() is main.m_problems


def test_tab_text_shows_problem_count_after_finish_model_refresh():
    main, result = _main_with_errors(
        ["not found: /a", "Permission denied: /b"])
    assert main.tabs.tabText(5) == f"Проблеми ({app._problem_count(result)})"
    assert main.tabs.tabText(5) == "Проблеми (2)"
    assert len(main.m_problems.categories) == 2


def test_empty_state_shows_no_problems_and_tab_has_no_number():
    result = core.scan([])  # порожній скан без помилок
    main = app.Main()
    main.result = result
    main._finish_model_refresh(result)
    assert main.tabs.tabText(5) == "Проблеми"
    assert main.m_problems.categories == []
    assert main.l_problems_status.text() == "Проблем не виявлено."


# ---- Блок 3: регресія IndexError на новій (6-й) вкладці --------------------

def test_switching_to_problems_tab_and_resizing_does_not_crash():
    """Регресія проти IndexError: (v_files..v_perceptual)[currentIndex()]
    у resizeEvent мав лише 5 елементів — на index 5 падав би. Проміжний
    assert на currentIndex()==5 навмисний: QTabWidget.setCurrentIndex()
    на неіснуючому індексі МОВЧКИ ігнорується (лишається на попередній
    вкладці) — без цього assert тест міг би "пройти", насправді
    перевіряючи вкладку 0, а не 5."""
    main, _result = _main_with_errors(["not found: /a"])
    main.tabs.setCurrentIndex(5)
    assert main.tabs.currentIndex() == 5
    _qapp.processEvents()
    resize = QResizeEvent(QSize(900, 700), QSize(800, 600))
    QApplication.sendEvent(main, resize)
    _qapp.processEvents()
    main._update_selection_summary()  # застереження: категорії не плутаються з подібністю


def test_switching_to_problems_tab_and_show_task_results_does_not_crash():
    """Той самий 5-елементний кортеж-пастка в _show_task (окрема гілка
    коду від resizeEvent, тому окремий тест)."""
    main, _result = _main_with_errors(["not found: /a"])
    main.tabs.setCurrentIndex(5)
    assert main.tabs.currentIndex() == 5
    _qapp.processEvents()
    main._show_task(workflow_ui.TASK_RESULTS)


def test_problems_tab_selection_disables_trash_actions():
    main, _result = _main_with_errors(["not found: /a"])
    main.tabs.setCurrentIndex(5)
    assert main.tabs.currentIndex() == 5
    idx = main.m_problems.index(0, 0)
    main.v_problems.setCurrentIndex(idx)
    main._update_selection_summary()
    main._update_action_state()
    assert not main.action_trash_current.isEnabled()
    assert not main.action_toggle_trash_mark.isEnabled()


# ---- Блок 4: структурний read-only — кожна деструктивна точка входу --------

class _AlwaysValidIndex:
    """Мінімальний дублер QModelIndex.isValid()==True: справжній валідний
    QModelIndex вимагав би реальних рядків моделі під ним."""

    def isValid(self) -> bool:  # noqa: N802 — Qt API
        return True


def test_delete_checked_refuses_problems_model():
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main.delete_checked(main.m_problems)


def test_delete_duplicate_paths_refuses_problems_model():
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main._delete_duplicate_paths(main.m_problems, {"/x"})


def test_verify_snapshot_group_then_trash_refuses_problems_model():
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main._verify_snapshot_group_then_trash(main.m_problems, {"/x"})


def test_trash_current_duplicate_refuses_when_active_model_is_problems(monkeypatch):
    """_active_model_view() навмисно НЕ включає ProblemsModel — цей тест
    імітує, наче хтось помилково приєднав його (майбутній рефакторинг), і
    підтверджує, що явна відмова спрацьовує, а не мовчазне "not
    GroupModel -> мабуть SimModel" (той самий прийом, що для Cluster/
    PerceptualModel у tests/test_perceptual_readonly_invariant.py)."""
    main = _main()
    monkeypatch.setattr(
        main, "_current_check_index",
        lambda: (main.m_problems, _AlwaysValidIndex(), "/some/path"))
    with pytest.raises(TypeError, match="GroupModel/SimModel"):
        main._trash_current_duplicate()


def test_trash_marked_on_current_tab_is_noop_on_problems_tab():
    """Реальний (не змодельований) шлях: справді перемкнутись на вкладку
    «Проблеми» через tabs.setCurrentIndex(5) і викликати toolbar-дію —
    _active_model_view() бачить лише files/dirs/sim, тож model=None і
    жодна isinstance-гілка не спрацьовує — тихий no-op, без винятку.

    Проміжний assert на currentIndex()==5 — не лише проти "хибного
    зеленого": без нової вкладки currentIndex() мовчки лишається на 0
    (файли), і _trash_marked_on_current_tab() пішов би РЕАЛЬНИМ шляхом
    delete_checked(m_files) із порожнім .checked — а це відкриває
    немодифікований QMessageBox.information() і вішає offscreen-прогін
    (див. попередження в ТЗ). Явний assert ловить це чисто, замість
    хангу."""
    main, _result = _main_with_errors(["not found: /a"])
    main.tabs.setCurrentIndex(5)
    assert main.tabs.currentIndex() == 5
    _qapp.processEvents()
    main._trash_marked_on_current_tab()


# ---- Блок 5: ПКМ-меню — лише «Показати у Finder»/«Копіювати повідомлення» --

class _FakeAction:
    def __init__(self, text):
        self.text_ = text
        self.enabled_ = True

    def setEnabled(self, value):  # noqa: N802 — Qt API
        self.enabled_ = value


class _FakeMenu:
    def __init__(self, *_a, **_kw):
        self.actions_: list[_FakeAction] = []

    def addAction(self, text):  # noqa: N802 — Qt API
        action = _FakeAction(text)
        self.actions_.append(action)
        return action

    def exec(self, *_a, **_kw):  # noqa: N802 — Qt API
        return None


def _build_problems_menu(main, idx):
    """Підмінити QMenu на фіктивний, побудувати ПКМ-меню, повернути
    {текст_дії: enabled} без модального .exec().

    Розгортає батька ПЕРЕД visualRect(): QTreeView лишає дочірні рядки
    згорнутими за замовчуванням, і visualRect() дочірнього рядка під
    згорнутим батьком не відповідає реальному екранному місцю — .center()
    тоді резолвиться в НЕВІРНИЙ (чи невалідний) рядок через indexAt(),
    і message_at()/шлях мовчки виходять None. Реальний клік ПКМ можливий
    лише по вже видимому (розгорнутому) рядку — те саме й тут."""
    parent = main.m_problems.parent(idx)
    if parent.isValid():
        main.v_problems.expand(parent)
    main.v_problems.setCurrentIndex(idx)
    captured: dict[str, "_FakeMenu"] = {}

    class _CapturingMenu(_FakeMenu):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["menu"] = self

    original_menu = app.QMenu
    app.QMenu = _CapturingMenu
    try:
        main._problems_menu(main.v_problems.visualRect(idx).center())
    finally:
        app.QMenu = original_menu
    menu = captured["menu"]
    return {a.text_: a.enabled_ for a in menu.actions_}


def test_problems_context_menu_has_no_destructive_actions(tmp_path):
    path = str(tmp_path / "a.txt")
    main, _result = _main_with_errors([f"not found: {path}"])
    child = main.m_problems.index(0, 0, main.m_problems.index(0, 0))
    actions = _build_problems_menu(main, child)
    assert actions, "меню не побудувалось"
    forbidden = {"кошик", "видал", "trash", "delete"}
    for text in actions:
        low = text.lower()
        assert not any(word in low for word in forbidden), (
            f"деструктивна дія на рядку проблеми: {text!r}")
    assert actions.get("Показати у Finder") is True
    assert actions.get("Копіювати повідомлення") is True


def test_problems_context_menu_reveal_disabled_without_extractable_path():
    main, _result = _main_with_errors(["дещо зламалося без шляху в тексті"])
    child = main.m_problems.index(0, 0, main.m_problems.index(0, 0))
    actions = _build_problems_menu(main, child)
    assert actions.get("Показати у Finder") is False
    assert actions.get("Копіювати повідомлення") is True


def test_problems_context_menu_on_category_row_disables_both_actions():
    """Категорія (кореневий рядок) — не повідомлення: message_at() дає
    None, тож обидві дії вимкнені (як dir_at()/path_at() для Cluster/
    PerceptualModel на кореневих рядках)."""
    main, _result = _main_with_errors(["not found: /a"])
    category_row = main.m_problems.index(0, 0)
    actions = _build_problems_menu(main, category_row)
    assert actions.get("Показати у Finder") is False
    assert actions.get("Копіювати повідомлення") is False


def test_problems_menu_ignores_click_outside_any_row():
    main, _result = _main_with_errors(["not found: /a"])
    invalid_point = main.v_problems.viewport().rect().bottomRight()
    # indexAt() на порожньому місці -> invalid QModelIndex -> рання
    # відмова; головне — що це не кидає винятку.
    main._problems_menu(invalid_point)


# ---- Блок 6: обережний парсер шляху з тексту помилки ------------------------

@pytest.mark.parametrize("message,expected", [
    ("/Users/x/dir: повторна перевірка не пройдена", "/Users/x/dir"),
    ("[Errno 13] Permission denied: '/Volumes/L/dir'", "/Volumes/L/dir"),
    ("не тека: /some/path", "/some/path"),
    ("iCloud-файл без локального вмісту — пропущено, щоб не викачувати "
     "з хмари: /a/b/c.heic", "/a/b/c.heic"),
    ("файл змінився під час читання", None),
    ("", None),
])
def test_first_path_in_message_parser(message, expected):
    assert app._first_path_in_message(message) == expected
