"""fskit-фантом: exFAT-драйвер macOS (fskit) перелічує запис через scandir,
але stat() падає ENOENT на цілому файлі (емпірично — кириличні NFD-імена на
/Volumes/L власника, fsck том OK). scan_one_dir мусить розрізняти це від
СПРАВЖНЬОГО зникнення (свіжий os.listdir усе ще бачить ім'я чи ні) і дати
точне, придатне до "Показати у Finder" повідомлення. Інваріант: зміна лише
тексту/категорії помилки — файл однаково виключений (stat однаково впав),
групування/хешування не зачіпаються.
"""

import errno
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.domain.product as product  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


class _FakeEntry:
    """Мінімальний двійник os.DirEntry: звичайний файл, чий stat() падає
    ENOENT (симулює fskit-фантом), як просить постановка."""

    def __init__(self, name: str, dirpath: str):
        self.name = name
        self.path = os.path.join(dirpath, name)

    def is_dir(self, follow_symlinks: bool = False) -> bool:
        return False

    def is_symlink(self) -> bool:
        return False

    def stat(self, follow_symlinks: bool = False):
        raise FileNotFoundError(
            errno.ENOENT, "No such file or directory", self.path)


class _FakeScandirCM:
    def __init__(self, entries):
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc_info):
        return False


def _patch_phantom(monkeypatch, tree, name: str, still_listed: bool):
    """Підміняє core.os.scandir(tree) на один фантомний запис і
    core.os.listdir на [name] (ще перелічений) чи [] (справді зник).
    Будь-який ІНШИЙ шлях — справжній os.scandir/os.listdir (обхід решти
    дерева не ламається)."""
    tree_abs = os.path.abspath(str(tree))
    real_scandir = core.os.scandir
    real_listdir = core.os.listdir

    def fake_scandir(path="."):
        if os.path.abspath(str(path)) == tree_abs:
            return _FakeScandirCM([_FakeEntry(name, tree_abs)])
        return real_scandir(path)

    def fake_listdir(path="."):
        if os.path.abspath(str(path)) == tree_abs:
            return [name] if still_listed else []
        return real_listdir(path)

    monkeypatch.setattr(core.os, "scandir", fake_scandir)
    monkeypatch.setattr(core.os, "listdir", fake_listdir)


# ---------------------------------------------------------------------------
# 1. Фантом присутній (listdir усе ще бачить ім'я) -> "драйвер не відкриває"
# ---------------------------------------------------------------------------

def test_fskit_phantom_gets_precise_message_and_stays_excluded(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    name = "Майно.pdf"  # й = и+U+0306 (NFD), точний кейс власника
    _patch_phantom(monkeypatch, tree, name, still_listed=True)

    # walk_threads=2 форсує walk_parallel/scan_one_dir незалежно від того,
    # що tmp_path — внутрішній диск (у бою це /Volumes/L, зовнішній exFAT,
    # де walk_parallel вмикається автоматично; тут форсуємо явно, щоб не
    # залежати від симуляції /Volumes/ у тестовому оточенні).
    res = core.scan([str(tree)], walk_threads=2)

    # ТОЧНА (не substring) звірка з os.path.abspath, як конструює
    # _FakeEntry.path у fake_scandir -- substring-перевірка типу
    # `"fskit" in message` тут НЕБЕЗПЕЧНА: назва tmp_path pytest сама
    # містить ім'я цієї тест-функції, тобто підрядок "fskit", і давала б
    # хибний ЗЕЛЕНИЙ навіть на старому коді (спіймано під час TDD-red).
    phantom_path = os.path.join(os.path.abspath(str(tree)), name)
    expected_message = (
        f"{phantom_path}: перелічений у теці, але "
        "macOS-драйвер exFAT не відкриває (fskit)"
    )
    matches = [e for e in res.errors if phantom_path in e]
    assert len(matches) == 1, res.errors
    message = matches[0]
    assert message == expected_message

    title, advice = product.classify_problem(message)
    assert title == "Драйвер не відкриває файл"
    assert "Paragon" in advice

    # інваріант: файл однаково виключений з доказу (stat впав так само)
    assert phantom_path not in res.file_meta
    assert res.file_groups == []
    assert res.dir_ok.get(str(tree)) is False


# ---------------------------------------------------------------------------
# 2. Справжнє зникнення (listdir теж НЕ бачить) -> поточна поведінка жива
# ---------------------------------------------------------------------------

def test_truly_vanished_entry_keeps_existing_classification(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    name = "Майно.pdf"
    _patch_phantom(monkeypatch, tree, name, still_listed=False)

    res = core.scan([str(tree)], walk_threads=2)

    phantom_path = os.path.join(os.path.abspath(str(tree)), name)
    expected_message = str(
        FileNotFoundError(errno.ENOENT, "No such file or directory", phantom_path))
    matches = [e for e in res.errors if phantom_path in e]
    assert len(matches) == 1, res.errors
    message = matches[0]
    # Точна звірка зі стандартним текстом OSError -- НЕ перекласифіковано,
    # (fskit) у повідомленні нема: справді зник, стара поведінка жива.
    assert message == expected_message
    assert "(fskit)" not in message

    title, _advice = product.classify_problem(message)
    assert title == "Елемент зник"
    assert phantom_path not in res.file_meta
    assert res.file_groups == []


# ---------------------------------------------------------------------------
# 3. product.classify_problem — юніт: "fskit" однозначно, не плутається з
#    generic "no such"/"not found"
# ---------------------------------------------------------------------------

def test_classify_problem_fskit_key_is_unambiguous():
    fskit_message = (
        "/Volumes/L/Telegram/Майно.pdf: перелічений у теці, але "
        "macOS-драйвер exFAT не відкриває (fskit)"
    )
    assert product.classify_problem(fskit_message)[0] == "Драйвер не відкриває файл"

    generic_message = "[Errno 2] No such file or directory: '/tmp/x.bin'"
    assert product.classify_problem(generic_message)[0] == "Елемент зник"


# ---------------------------------------------------------------------------
# 4. Скан без помилок — результат не змінився
# ---------------------------------------------------------------------------

def test_normal_scan_without_errors_is_unaffected(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 1000)
        make(tmp_path / top / "y.bin", b"Y" * 2000)
    make(tmp_path / "solo.bin", b"S" * 300)

    def canon(res):
        return (
            sorted((g.digest, tuple(sorted(g.paths))) for g in res.file_groups),
            sorted(tuple(sorted(g.paths)) for g in res.dir_groups),
            res.files_seen,
            res.bytes_seen,
            res.errors,
        )

    sequential = core.scan([str(tmp_path)], walk_threads=1)
    parallel = core.scan([str(tmp_path)], walk_threads=2)
    default = core.scan([str(tmp_path)])
    assert canon(sequential) == canon(parallel) == canon(default)
    assert sequential.errors == []
