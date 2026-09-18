"""Автофікс імен fskit-фантомів + NFD-профілактика.

НАЙВАЖЛИВІШЕ ЕМПІРИЧНЕ ВІДКРИТТЯ (зроблено готуючи цей файл, на реальному
hdiutil-змонтованому exFAT-образі — те саме середовище, що
test_fs_matrix*.py): renamex_np(..., RENAME_EXCL) на exFAT/fskit
підтримується лише ЧАСТКОВО. Контрольовано, по 3 незалежні спроби на
кожен бік:
  ціль ВЖЕ існує -> завжди EEXIST (безпечно, як і мало бути);
  ціль ВІДСУТНЯ  -> завжди ENOTSUP (3/3) — драйвер НЕ вміє атомарного
                    "створити-якщо-відсутнє", хоча коректно розпізнає
                    наявну ціль.
Без фолбека це робило б outcome "fixed" НЕДОСЯЖНИМ на exFAT — саме тій
ФС, заради якої фіча існує. fsops.fix_name_to_nfc тому: пробує EXCL;
якщо ENOTSUP/EOPNOTSUPP (а НЕ EEXIST) — це вже підтвердження відсутності
цілі (інакше перша спроба дала б EEXIST) — і лише ТОДІ пробує звичайний
rename. Це лишає короткий (два системні виклики поспіль, без затримки)
TOCTOU-проміжок, немислимий без апаратної/драйверної підтримки атомарної
винятковості — прийнятний для одноразової ручної дії одного користувача.
Деталі й контрольований експеримент — коментар над fsops._RENAME_EXCL.

ДРУГЕ відкриття: на цьому ж exFAT-образі (як і на APFS) macOS ФОЛДИТЬ
NFD/NFC-форми ОДНОГО рядка до ОДНОГО запису на рівні open()/lstat() —
створити ДВІ окремі сутності (одну NFD-байтами, одну NFC-байтами)
звичайними файловими викликами НЕ вдається (див.
test_nfd_and_nfc_forms_fold_to_the_same_entry_on_real_exfat нижче, що
документує це напряму). Тому мандатний тест «ціль існує -> exists,
обидва файли цілі», який постановка описує через СПРАВЖНЄ створення
обох форм, конструктивно неможливий на жодному доступному в пісочниці
диску — ні тут, ні на tmp_path. Замінено на monkeypatch нижнього рівня
(fsops._renamex_np кидає EEXIST) — той самий прийом, який постановка й
так явно санкціонує для "phantom", застосований так само й до "exists".
Обидва (phantom і genuinely-two-colliding-entries) в принципі
відтворювані лише зіпсованим/невідповідним станом на боці драйвера
(точно як другий, необхідний для колізії файл — не той, що народжується
звичайним записом macOS), а не звичайним створенням файла — так само,
як CI не може створити СПРАВЖНІЙ fskit-фантом.

ТРЕТЄ відкриття (найглибше): цей exFAT/fskit-драйвер сам перекодовує
КОЖЕН запис, що через нього пишеться, у розкладену (NFD) форму на
диску — незалежно від байтів, що передаєш у open()/rename(). Перевірено
й ПРЯМИМ створенням файла під NFC-байтами (жодного стосунку до rename):
на диску все одно лягає NFD (test_all_writes_land_as_nfd_on_this_exfat_
mount). Наслідок для fix_name_to_nfc: після "fixed" неможливо (на цій
ФС) звірити on-disk ім'я побайтово проти NFC — воно ЗАВЖДИ фактично
NFD, хай що передано в rename(). Це НЕ шкодить меті фічі: файл
доступний під ОБОМА байтовими формами (лукап нормалізовано-нечутливий
в обидва боки — друге відкриття), тож тест 1 нижче звіряє доступність і
цілісність, а не точний байтовий запис. "detail" на "fixed" — запитане
ім'я, яким файл ГАРАНТОВАНО адресується, а не обіцянка щодо байтів на
диску.
"""

import errno
import os
import sys
import tempfile
import unicodedata

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
import dupscan.infra.preferences as preferences  # noqa: E402
from qa.fs_images import hdiutil_available, mount_image, unmount  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _nfc_nfd_pair(base: str = "Майно.pdf") -> tuple[str, str]:
    """(NFC, NFD) — самоперевірна пара: якщо *base* колись перестане мати
    декомпоновану форму (інша строка), тест впаде тут, а не мовчки
    протестує щось інше."""
    nfc = unicodedata.normalize("NFC", base)
    nfd = unicodedata.normalize("NFD", base)
    assert nfd != nfc, "потрібне ім'я з розкладною послідовністю"
    return nfc, nfd


class _ScriptedRenamex:
    """Фіктивний fsops._renamex_np: список errno (чи None = успіх) по
    черзі на кожен виклик. Записує кожен виклик для перевірки аргументів
    і кількості (напр. "фолбек НІКОЛИ не викликається на EEXIST")."""

    def __init__(self, script: list[int | None]):
        self.script = list(script)
        self.calls: list[tuple[str, str, int]] = []

    def __call__(self, old_path: str, new_path: str, flags: int) -> None:
        self.calls.append((old_path, new_path, flags))
        outcome = self.script.pop(0)
        if outcome is not None:
            raise OSError(outcome, os.strerror(outcome), old_path)


# =============================================================================
# Блок 1: fix_name_to_nfc — чиста логіка (monkeypatch _renamex_np), без
# реального диска поза tmp_path (протокол/дерево рішень, не сама ФС).
# =============================================================================

def test_already_nfc_is_noop_and_never_touches_disk(monkeypatch, tmp_path):
    nfc, _nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([])  # порожній скрипт: будь-який виклик -> IndexError
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfc)
    assert (outcome, detail) == ("already-nfc", nfc)
    assert fake.calls == []


def test_protected_under_reference_root_blocks_before_any_rename(
        monkeypatch, tmp_path):
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    preferences.save_reference_roots([str(tmp_path)])
    outcome, _detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "protected"
    assert fake.calls == []


def test_protected_when_reference_roots_config_unreadable_fails_closed(
        monkeypatch, tmp_path):
    """Fail-closed, той самий інваріант, що to_trash: не можемо
    підтвердити відсутність еталона -> не перейменовуємо."""
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([])
    monkeypatch.setattr(fsops, "_renamex_np", fake)

    def broken_load(*_a, **_kw):
        raise preferences.PreferencesError("biту JSON")

    monkeypatch.setattr(preferences, "load_reference_roots", broken_load)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "protected"
    assert "не читається" in detail
    assert fake.calls == []


def test_phantom_on_enoent(monkeypatch, tmp_path):
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([errno.ENOENT])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert (outcome, detail) == ("phantom", "")
    assert len(fake.calls) == 1


def test_exists_on_eexist_never_attempts_fallback(monkeypatch, tmp_path):
    """§4: нуль перезапису. EEXIST на ПЕРШІЙ (EXCL) спробі -> "exists"
    негайно; фолбек (rename без EXCL, теоретично здатний перезаписати)
    НІКОЛИ не викликається — рівно один виклик _renamex_np."""
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([errno.EEXIST])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert (outcome, detail) == ("exists", "")
    assert len(fake.calls) == 1
    assert fake.calls[0][2] == fsops._RENAME_EXCL


def test_error_on_other_oserror(monkeypatch, tmp_path):
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([errno.EACCES])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "error"
    assert detail  # текст помилки присутній
    assert len(fake.calls) == 1


def test_error_when_renamex_np_libc_unavailable(monkeypatch, tmp_path):
    """_renamex_np сам підіймає ENOSYS, коли libc/символ недоступні (не-
    macOS чи пісочниця) — той самий шлях, катається як "error"."""
    _nfc, nfd = _nfc_nfd_pair()
    monkeypatch.setattr(fsops, "_renamex_np_libc", lambda: None)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "error"
    assert "недоступний" in detail


@pytest.mark.parametrize("unsupported_errno", [errno.ENOTSUP, errno.EOPNOTSUPP])
def test_falls_back_to_plain_rename_when_excl_unsupported(
        monkeypatch, tmp_path, unsupported_errno):
    """Ядро §4-безпечного фолбека (див. модульний докстрінг): ENOTSUP/
    EOPNOTSUPP -> другий виклик БЕЗ EXCL -> "fixed" при успіху. Перевіряє
    і аргументи (flags=0 на другому виклику), і що дійшло до "fixed"."""
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([unsupported_errno, None])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert (outcome, detail) == ("fixed", unicodedata.normalize("NFC", nfd))
    assert len(fake.calls) == 2
    assert fake.calls[0][2] == fsops._RENAME_EXCL
    assert fake.calls[1][2] == 0


def test_fallback_still_reports_exists_on_late_collision(monkeypatch, tmp_path):
    """Гонитва (щось з'явилось МІЖ двома системними викликами) — фолбек
    сам ловить EEXIST і чесно повертає "exists", а не крашиться чи
    мовчки перезаписує."""
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([errno.ENOTSUP, errno.EEXIST])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, _detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "exists"
    assert len(fake.calls) == 2


def test_fallback_error_path(monkeypatch, tmp_path):
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([errno.ENOTSUP, errno.EACCES])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "error"
    assert detail
    assert len(fake.calls) == 2


def test_fallback_phantom_path(monkeypatch, tmp_path):
    """ENOTSUP на першій спробі, ENOENT на фолбеку — теж чесний "phantom",
    не "error": ФС визнала відсутність цілі, а потім і джерела."""
    _nfc, nfd = _nfc_nfd_pair()
    fake = _ScriptedRenamex([errno.ENOTSUP, errno.ENOENT])
    monkeypatch.setattr(fsops, "_renamex_np", fake)
    outcome, _detail = fsops.fix_name_to_nfc(str(tmp_path), nfd)
    assert outcome == "phantom"


# =============================================================================
# Блок 2: примітив на РЕАЛЬНОМУ exFAT-образі (skipif DUPSCAN_FS_MATRIX) —
# та сама ФС, що test_fs_matrix*.py.
# =============================================================================

pytestmark_exfat = pytest.mark.skipif(
    os.environ.get("DUPSCAN_FS_MATRIX") != "1" or not hdiutil_available(),
    reason="потрібен hdiutil: увімкнути DUPSCAN_FS_MATRIX=1",
)


@pytest.fixture
def exfat_dir():
    _image, mount_point = mount_image("ExFAT", size_mb=32)
    try:
        yield mount_point
    finally:
        unmount(mount_point)


@pytestmark_exfat
def test_fixed_on_lone_nfd_file_real_exfat(exfat_dir):
    """Профілактика: свіжий, повністю доступний NFD-файл (не фантом —
    справжній фантом на CI не відтворити, див. модульний докстрінг)
    після fsops.fix_name_to_nfc лишається доступним, цілим, ОДНИМ
    записом — та сама фізична сутність (inode).

    НЕ звіряє байти on-disk імені проти NFC буквально: третє емпіричне
    відкриття (див. test_all_writes_land_as_nfd_on_this_exfat_mount
    нижче) — цей exFAT/fskit-драйвер сам перекодовує КОЖЕН запис у NFD
    на диску, незалежно від байтів, що передаєш у rename()/open(). Це
    не шкодить меті фічі (файл ДОСТУПНИЙ — lookup нормалізовано-
    нечутливий в обидва боки, перевірено нижче), лише означає, що
    "detail" ("нове NFC ім'я") описує ЗАПИТАНЕ ім'я, яким файл
    ГАРАНТОВАНО адресується, а не обов'язково точний байтовий запис на
    диску."""
    name_nfc, name_nfd = _nfc_nfd_pair()
    d = os.path.join(exfat_dir, "sub")
    os.makedirs(d)
    path_nfd = os.path.join(d, name_nfd)
    with open(path_nfd, "wb") as fh:
        fh.write(b"exact content 1")
    before_ino = os.lstat(path_nfd).st_ino

    outcome, detail = fsops.fix_name_to_nfc(d, name_nfd)

    assert outcome == "fixed"
    assert detail == name_nfc
    entries = [n for n in os.listdir(d) if not n.startswith("._")]
    assert len(entries) == 1, "рівно один запис — жодного дублювання"
    path_nfc = os.path.join(d, name_nfc)
    for probe in (path_nfc, path_nfd):
        after = os.lstat(probe)
        assert after.st_ino == before_ino, "той самий файл, лише інше ім'я"
        with open(probe, "rb") as fh:
            assert fh.read() == b"exact content 1"


def test_all_writes_land_as_nfd_on_this_exfat_mount(exfat_dir):
    """ТРЕТЄ емпіричне відкриття (готуючи цей файл): цей exFAT/fskit-
    драйвер сам перекодовує КОЖЕН запис у розкладену (NFD) форму на
    диску — незалежно від байтів, що передаєш у open()/rename(). Пряме
    створення файла під NFC-байтами (жодного відношення до rename)
    лягає на диск як NFD. Разом із другим відкриттям (NFD/NFC фолдяться
    в ОДИН lookup-запис — див. test_nfd_and_nfc_forms_fold_to_the_same_
    entry_on_real_exfat) це пояснює, чому fix_name_to_nfc не можна
    звіряти по точних байтах on-disk імені (тест вище) — і чому
    "детально верифікований NFC-запис" на цьому класі ФС структурно
    недосяжний: НЕ баг fix_name_to_nfc, властивість самого драйвера."""
    name_nfc, _name_nfd = _nfc_nfd_pair()
    d = os.path.join(exfat_dir, "sub")
    os.makedirs(d)
    path_nfc_requested = os.path.join(d, name_nfc)
    with open(path_nfc_requested, "wb") as fh:
        fh.write(b"direct nfc write, unrelated to rename")
    entries = [n for n in os.listdir(d) if not n.startswith("._")]
    assert len(entries) == 1
    assert entries[0] != name_nfc, (
        "якщо це коли-небудь стане True — драйвер більше НЕ форсує NFD, "
        "і fix_name_to_nfc варто переглянути (можливо, вже й НЕ треба "
        "фолбек-логіки на ENOTSUP)")
    assert unicodedata.normalize("NFD", entries[0]) == entries[0]


@pytestmark_exfat
def test_already_nfc_real_exfat_mtime_and_inode_unchanged(exfat_dir):
    name_nfc, _name_nfd = _nfc_nfd_pair()
    d = os.path.join(exfat_dir, "sub")
    os.makedirs(d)
    path = os.path.join(d, name_nfc)
    with open(path, "wb") as fh:
        fh.write(b"already canonical")
    before = os.lstat(path)

    outcome, detail = fsops.fix_name_to_nfc(d, name_nfc)

    assert (outcome, detail) == ("already-nfc", name_nfc)
    after = os.lstat(path)
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


@pytestmark_exfat
def test_protected_reference_root_real_exfat(exfat_dir):
    name_nfc, name_nfd = _nfc_nfd_pair()
    d = os.path.join(exfat_dir, "sub")
    os.makedirs(d)
    path_nfd = os.path.join(d, name_nfd)
    with open(path_nfd, "wb") as fh:
        fh.write(b"protected content")
    preferences.save_reference_roots([d])

    outcome, _detail = fsops.fix_name_to_nfc(d, name_nfd)

    assert outcome == "protected"
    entries = [n for n in os.listdir(d) if not n.startswith("._")]
    assert entries == [name_nfd], "еталон: ім'я НЕ мало змінитись"
    with open(path_nfd, "rb") as fh:
        assert fh.read() == b"protected content"


@pytestmark_exfat
def test_nfd_and_nfc_forms_fold_to_the_same_entry_on_real_exfat(exfat_dir):
    """ДОКУМЕНТУЄ емпіричне відкриття (див. модульний докстрінг): macOS
    (open()/lstat(), навіть на exFAT) розв'язує NFD- і NFC-байтову форму
    ОДНОГО рядка в ОДИН і той самий запис — genuinely-дві-окремі-сутності
    звичайним os.open()/rename() тут НЕ конструюються. Це причина, чому
    "ціль існує" (наступний блок тестів) верифікується monkeypatch-ем
    нижнього рівня, а не буквальним створенням "і NFD, і NFC" файлів."""
    name_nfc, name_nfd = _nfc_nfd_pair()
    d = os.path.join(exfat_dir, "sub")
    os.makedirs(d)
    path_nfd = os.path.join(d, name_nfd)
    path_nfc = os.path.join(d, name_nfc)
    with open(path_nfd, "wb") as fh:
        fh.write(b"x")

    # lstat під ІНШОЮ (NFC) байтовою формою вже резолвиться в ТОЙ САМИЙ inode
    assert os.lstat(path_nfc).st_ino == os.lstat(path_nfd).st_ino

    # окреме, ЕКСКЛЮЗИВНЕ створення під NFC-формою -> EEXIST, не другий файл
    with pytest.raises(FileExistsError):
        with open(path_nfc, "xb"):
            pass


# =============================================================================
# Блок 3: UI вкладки «Проблеми» — ПКМ-пункт і bulk-кнопка.
# =============================================================================

def _main() -> app.Main:
    main = app.Main()
    main.result = core.scan([])
    return main


def _main_with_errors(errors: list[str]):
    result = core.ScanResult()
    result.errors = list(errors)
    result.errors_total = len(errors)
    main = app.Main()
    main.result = result
    main._finish_model_refresh(result)
    _qapp.processEvents()
    return main, result


class _FakeAction:
    def __init__(self, text):
        self.text_ = text
        self.enabled_ = True

    def setEnabled(self, value):  # noqa: N802 — Qt API
        self.enabled_ = value


class _FakeMenu:
    def __init__(self, *_a, **_kw):
        self.actions_: list[_FakeAction] = []
        self._chosen: _FakeAction | None = None

    def addAction(self, text):  # noqa: N802 — Qt API
        action = _FakeAction(text)
        self.actions_.append(action)
        return action

    def exec(self, *_a, **_kw):  # noqa: N802 — Qt API
        return self._chosen


def _build_problems_menu(main, idx):
    """Той самий прийом, що tests/test_problems_tab.py::_build_problems_menu
    (розгортання батька перед visualRect — інакше клік резолвиться в
    невірний рядок): підмінити QMenu, побудувати ПКМ-меню, повернути
    {текст: enabled} без модального .exec() (лише перевірка стану меню —
    для власне "кліку" є _click_problems_menu_action нижче)."""
    parent = main.m_problems.parent(idx)
    if parent.isValid():
        main.v_problems.expand(parent)
    main.v_problems.setCurrentIndex(idx)
    captured: dict[str, _FakeMenu] = {}

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


def _click_problems_menu_action(main, idx, action_text: str):
    """На відміну від _build_problems_menu (лише читає стан меню), тут
    .exec() одразу повертає обрану дію — власне "клік" ПКМ-пункту."""
    parent = main.m_problems.parent(idx)
    if parent.isValid():
        main.v_problems.expand(parent)
    main.v_problems.setCurrentIndex(idx)

    class _ClickingMenu(_FakeMenu):
        def exec(self, *_a, **_kw):  # noqa: N802 — Qt API
            for a in self.actions_:
                if a.text_ == action_text:
                    return a
            return None

    original_menu = app.QMenu
    app.QMenu = _ClickingMenu
    try:
        main._problems_menu(main.v_problems.visualRect(idx).center())
    finally:
        app.QMenu = original_menu


def test_namefix_context_action_enabled_only_on_eligible_message_rows(tmp_path):
    fskit_path = str(tmp_path / "a.pdf")
    vanished_path = str(tmp_path / "b.pdf")
    denied_path = str(tmp_path / "c.pdf")
    main, _result = _main_with_errors([
        f"{fskit_path}: перелічений у теці, але macOS-драйвер exFAT не "
        "відкриває (fskit)",
        f"not found: {vanished_path}",
        f"Permission denied: {denied_path}",
    ])
    by_title = {c.title: i for i, c in enumerate(main.m_problems.categories)}

    fskit_row = main.m_problems.index(
        0, 0, main.m_problems.index(by_title["Драйвер не відкриває файл"], 0))
    actions = _build_problems_menu(main, fskit_row)
    assert actions["Спробувати полагодити ім'я (NFC)"] is True

    vanished_row = main.m_problems.index(
        0, 0, main.m_problems.index(by_title["Елемент зник"], 0))
    actions = _build_problems_menu(main, vanished_row)
    assert actions["Спробувати полагодити ім'я (NFC)"] is True

    denied_row = main.m_problems.index(
        0, 0, main.m_problems.index(by_title["Немає доступу"], 0))
    actions = _build_problems_menu(main, denied_row)
    assert actions["Спробувати полагодити ім'я (NFC)"] is False

    category_row = main.m_problems.index(by_title["Елемент зник"], 0)
    actions = _build_problems_menu(main, category_row)
    assert actions["Спробувати полагодити ім'я (NFC)"] is False


def test_namefix_context_action_calls_primitive_with_split_dirpath_and_name(
        monkeypatch, tmp_path):
    """Шлях свідомо з пробілом у назві теки («Мої документи») — саме той
    формат, що старий _first_path_in_message обрізав би (див. правку
    регексу вище в app.py), даючи хибний dirpath/name тут. _bg_run
    підмінено на синхронний виклик — детермінізм замість реального
    QThread + processEvents-очікування; окремо перевіряє, що дія ЙДЕ
    через _bg_run (постановка, п.1), а не напряму в GUI-потоці."""
    target = tmp_path / "Мої документи"
    target.mkdir()
    full_path = str(target / "Майно.pdf")
    main, _result = _main_with_errors([f"not found: {full_path}"])
    calls = []

    def fake_fix(dirpath, name):
        calls.append((dirpath, name))
        return "fixed", "Майно.pdf"

    monkeypatch.setattr(fsops, "fix_name_to_nfc", fake_fix)
    bg_run_calls = []

    def spying_bg_run(self, fn, on_ok, on_err=None):
        bg_run_calls.append(1)
        on_ok(fn())

    monkeypatch.setattr(app.Main, "_bg_run", spying_bg_run)
    row = main.m_problems.index(0, 0, main.m_problems.index(0, 0))
    _click_problems_menu_action(main, row, "Спробувати полагодити ім'я (NFC)")

    assert calls == [(str(target), "Майно.pdf")]
    assert len(bg_run_calls) == 1
    assert "полагоджено" in main.status.text().lower()


@pytest.mark.parametrize("outcome,fragment", [
    ("fixed", "полагоджено"),
    ("phantom", "не адресує"),
    ("exists", "уже існує"),
    ("already-nfc", "вже канонічне"),
    ("protected", "еталон"),
    ("error", "помилка"),
])
def test_namefix_status_text_matches_outcome(outcome, fragment):
    text = app._namefix_status_text(outcome, "деталь")
    assert fragment in text.lower()


def test_bulk_button_enabled_only_when_active_category_eligible(tmp_path):
    main, _result = _main_with_errors([
        f"not found: {tmp_path / 'a.pdf'}",
        f"Permission denied: {tmp_path / 'b.pdf'}",
    ])
    assert main.b_problems_namefix_bulk.isEnabled() is False
    by_title = {c.title: i for i, c in enumerate(main.m_problems.categories)}

    eligible_idx = main.m_problems.index(by_title["Елемент зник"], 0)
    main.v_problems.setCurrentIndex(eligible_idx)
    main._update_problems_namefix_button()
    assert main.b_problems_namefix_bulk.isEnabled() is True

    other_idx = main.m_problems.index(by_title["Немає доступу"], 0)
    main.v_problems.setCurrentIndex(other_idx)
    main._update_problems_namefix_button()
    assert main.b_problems_namefix_bulk.isEnabled() is False


def test_bulk_namefix_asks_confirmation_before_touching_primitive(
        monkeypatch, tmp_path):
    main, _result = _main_with_errors([f"not found: {tmp_path / 'a.pdf'}"])
    idx = main.m_problems.index(0, 0)
    main.v_problems.setCurrentIndex(idx)
    calls = []
    monkeypatch.setattr(fsops, "fix_name_to_nfc",
                         lambda d, n: calls.append((d, n)) or ("fixed", n))
    monkeypatch.setattr(
        app.QMessageBox, "question",
        lambda *a, **kw: app.QMessageBox.StandardButton.No)
    main._problems_namefix_bulk()
    assert calls == [], "відмова у підтвердженні -> примітив не викликається"


def test_bulk_namefix_noop_when_active_category_not_eligible(monkeypatch, tmp_path):
    main, _result = _main_with_errors([f"Permission denied: {tmp_path / 'a.pdf'}"])
    idx = main.m_problems.index(0, 0)
    main.v_problems.setCurrentIndex(idx)
    calls = []
    monkeypatch.setattr(fsops, "fix_name_to_nfc",
                         lambda d, n: calls.append((d, n)) or ("fixed", n))
    asked = []
    monkeypatch.setattr(
        app.QMessageBox, "question",
        lambda *a, **kw: asked.append(1) or app.QMessageBox.StandardButton.Yes)
    main._problems_namefix_bulk()
    assert calls == []
    assert asked == [], "неприйнятна категорія -> діалог навіть не питає"


def test_bulk_namefix_noop_when_no_extractable_paths(monkeypatch):
    main, _result = _main_with_errors(["not found: немає тут абсолютного шляху"])
    idx = main.m_problems.index(0, 0)
    main.v_problems.setCurrentIndex(idx)
    calls = []
    monkeypatch.setattr(fsops, "fix_name_to_nfc",
                         lambda d, n: calls.append((d, n)) or ("fixed", n))
    informed = []
    monkeypatch.setattr(
        app.QMessageBox, "information",
        lambda *a, **kw: informed.append(a))
    main._problems_namefix_bulk()
    assert calls == []
    assert informed, "має повідомити, що видобувати нічого"


def test_bulk_namefix_counts_scripted_outcomes_and_uses_bg_run(
        monkeypatch, tmp_path):
    """«fake-примітив зі скриптованими результатами» (постановка) —
    перевіряє одразу три речі: (1) підсумок правильно рахує ЗАДАНІ
    outcome-и; (2) виконання йде через _bg_run (не напряму, не окремим
    QThread); (3) підтвердження було запитано перед стартом."""
    paths = [str(tmp_path / f"f{i}.pdf") for i in range(5)]
    main, _result = _main_with_errors([f"not found: {p}" for p in paths])
    idx = main.m_problems.index(0, 0)
    main.v_problems.setCurrentIndex(idx)

    scripted = iter(["fixed", "fixed", "phantom", "exists", "error"])

    def fake_fix(dirpath, name):
        outcome = next(scripted)
        return outcome, ("новеім'я" if outcome == "fixed" else "")

    monkeypatch.setattr(fsops, "fix_name_to_nfc", fake_fix)
    monkeypatch.setattr(
        app.QMessageBox, "question",
        lambda *a, **kw: app.QMessageBox.StandardButton.Yes)
    captured_summary = []
    monkeypatch.setattr(
        app.QMessageBox, "information",
        lambda *a, **kw: captured_summary.append(a[-1]))

    bg_run_calls = []

    def spying_bg_run(self, fn, on_ok, on_err=None):
        bg_run_calls.append(1)
        on_ok(fn())  # синхронно в тесті — головне, що ПІШЛО через _bg_run

    monkeypatch.setattr(app.Main, "_bg_run", spying_bg_run)

    main._problems_namefix_bulk()

    assert len(bg_run_calls) == 1, "має пройти через _bg_run рівно один раз"
    assert captured_summary, "підсумковий діалог мав з'явитись"
    summary = captured_summary[0]
    assert "2" in summary and "фантом" in summary.lower()
    assert "1" in summary  # exists=1, помилок=1 (обидва рахуються по 1)
    assert "повторіть сканування" in summary.lower()


def test_bulk_namefix_best_effort_one_failure_does_not_stop_the_rest(
        monkeypatch, tmp_path):
    """Best-effort (постановка, п.2 UI): несподіваний виняток на ОДНОМУ
    файлі не має зупинити обробку решти."""
    paths = [str(tmp_path / f"f{i}.pdf") for i in range(3)]
    main, _result = _main_with_errors([f"not found: {p}" for p in paths])
    idx = main.m_problems.index(0, 0)
    main.v_problems.setCurrentIndex(idx)

    calls = []

    def flaky_fix(dirpath, name):
        calls.append((dirpath, name))
        if len(calls) == 2:
            raise RuntimeError("несподіваний збій саме на другому файлі")
        return "fixed", name

    monkeypatch.setattr(fsops, "fix_name_to_nfc", flaky_fix)
    monkeypatch.setattr(
        app.QMessageBox, "question",
        lambda *a, **kw: app.QMessageBox.StandardButton.Yes)
    captured_summary = []
    monkeypatch.setattr(
        app.QMessageBox, "information",
        lambda *a, **kw: captured_summary.append(a[-1]))
    monkeypatch.setattr(
        app.Main, "_bg_run",
        lambda self, fn, on_ok, on_err=None: on_ok(fn()))

    main._problems_namefix_bulk()

    assert len(calls) == 3, "усі три мали бути спробувані, попри збій на 2-му"
    summary = captured_summary[0]
    assert "полагоджено 2" in summary.lower()
    assert "помилок 1" in summary.lower()


def test_problems_tab_read_only_invariant_unaffected_by_namefix(tmp_path):
    """Регресія: нові ПКМ-пункт/bulk-кнопка НЕ відкрили новий шлях у
    Кошик — ProblemsModel і далі відхиляється кожною деструктивною
    точкою (докладно в tests/test_problems_tab.py; тут — прямий доказ,
    що саме ЦЕЙ файл нічого не послабив)."""
    main = _main()
    with pytest.raises(TypeError, match="GroupModel"):
        main.delete_checked(main.m_problems)
    assert not hasattr(main.m_problems, "checked")
