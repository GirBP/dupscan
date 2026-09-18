"""Супутники AppleDouble («._X») невидимі для скану, плану і доказів.

Регресія знайдена матрицею ФС на РЕАЛЬНОМУ exFAT-образі
(tests/test_fs_matrix_merge.py), але сам контракт — суто про імена, тому
відтворюється на будь-якій ФС звичайними файлами «._X» і ходить у гейті
завжди, без hdiutil. Так правило лишається під охороною навіть там, де
матрицю ФС не вмикали.

Три дефекти, які ці тести не дають повернути (усі спостережені на exFAT):
  1. супутники збиралися у фальшиву групу дублікатів — «звільнення» знищило б
     метадані живих файлів;
  2. merge_plan клав у план і X, і «._X», а ядро переносить супутник разом з X
     → рядок «._X» падав [Errno 2] («N файл(ів) не перенесено»);
  3. гейт Кошика вимагав доказу дубліката для супутника і блокував відправку
     цілком доказаної теки.
"""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.app as app_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402

_qapp = QApplication.instance() or QApplication([])

# Справжня «шапка» AppleDouble: магія 0x00051607 + версія.
_APPLEDOUBLE_HEADER = bytes.fromhex("0005160700020000") + b"\0" * 4088


def _sidecar(path) -> None:
    """Покласти поруч із файлом його супутник, як це робить macOS на exFAT."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_APPLEDOUBLE_HEADER)


def _looks_like_sidecar(name: str) -> bool:
    """Незалежний оракул тесту.

    Свідомо НЕ core.is_appledouble: перевірка, написана через функцію, яку вона
    ж і перевіряє, залишається зеленою навіть коли правило вимкнене — саме так
    один із цих тестів спершу й виявився порожнім.
    """
    return name.startswith("._")


def test_is_appledouble_contract():
    assert core.is_appledouble("._x.bin")
    assert core.is_appledouble("._")
    # Звичайні приховані файли — НЕ супутники: «.gitignore» лишається файлом.
    assert not core.is_appledouble(".gitignore")
    assert not core.is_appledouble("x.bin")
    assert not core.is_appledouble("a._b")


def test_scan_never_groups_sidecars(tmp_path):
    """Однакові супутники не мають ставати групою дублікатів."""
    payload = os.urandom(64 * 1024)
    for top in ("A", "B"):
        directory = tmp_path / top
        directory.mkdir()
        (directory / "f.bin").write_bytes(payload)
        _sidecar(directory / "._f.bin")
    _sidecar(tmp_path / "._A")
    _sidecar(tmp_path / "._B")

    result = core.scan([str(tmp_path)])

    assert len(result.file_groups) == 1, (
        "супутники не мають утворювати другу, фальшиву групу")
    assert sorted(os.path.basename(p) for p in result.file_groups[0].paths) == [
        "f.bin", "f.bin"]
    assert not any(
        _looks_like_sidecar(os.path.basename(path))
        for group in result.file_groups for path in group.paths)


def test_merge_plan_omits_sidecars(tmp_path):
    """Супутник не потрапляє в план: ядро перенесе його разом із файлом."""
    source = tmp_path / "src"
    destination = tmp_path / "dst"
    source.mkdir()
    destination.mkdir()
    (source / "unique.bin").write_bytes(os.urandom(5000))
    _sidecar(source / "._unique.bin")
    shared = os.urandom(64 * 1024)
    (source / "shared.bin").write_bytes(shared)
    (destination / "shared.bin").write_bytes(shared)

    result = core.scan([str(tmp_path)])
    plan, _total = core.merge_plan(result, str(source), str(destination))

    planned = {rel for _size, _src, rel in plan}
    assert "unique.bin" in planned, "справжній унікальний файл мусить переїхати"
    assert not any(_looks_like_sidecar(os.path.basename(rel)) for rel in planned), (
        f"супутник не мав потрапити в план: {sorted(planned)}")


def test_directory_proof_ignores_sidecars(tmp_path):
    """Дві теки з однаковим вмістом доказово рівні, навіть якщо супутники різні."""
    left = tmp_path / "left"
    right = tmp_path / "right"
    payload = os.urandom(4096)
    for directory in (left, right):
        directory.mkdir()
        (directory / "f.bin").write_bytes(payload)
    _sidecar(left / "._f.bin")  # супутник лише з одного боку

    left_digest, _lb, left_files = core.snapshot_directory(str(left))
    right_digest, _rb, right_files = core.snapshot_directory(str(right))

    assert left_digest == right_digest, (
        "супутник не має робити теки різними — на APFS ці метадані невидимі")
    assert left_files == right_files == 1


def test_trash_gate_not_blocked_by_sidecar(tmp_path, monkeypatch):
    """Доказана тека їде в Кошик, хоч супутник доказу дубліката не має."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    source = tmp_path / "copy"
    destination = tmp_path / "original"
    payload = os.urandom(64 * 1024)
    for directory in (source, destination):
        directory.mkdir()
        (directory / "f.bin").write_bytes(payload)
    _sidecar(source / "._f.bin")  # супутник лише в теці, яку відправляємо

    result = core.scan([str(tmp_path)])

    trashed: list = []

    def recorder(victims, **_kwargs):
        trashed.extend(victims)
        return []

    monkeypatch.setattr(app_mod, "to_trash", recorder)

    kind, errors = app_mod._verify_then_trash_dir(result, str(source))

    assert (kind, errors) == ("ok", []), (
        "супутник не має блокувати Кошик для доказаної теки")
    assert trashed == [str(source)]
