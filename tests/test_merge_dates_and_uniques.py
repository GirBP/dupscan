"""Дати й справжня унікальність при злитті тек.

Три дірки, знайдені на реальному диску власника (exFAT, /Volumes/L):

1. Копія втрачала дату ЗМІНИ на exFAT. `os.utime` стояв на дескрипторі
   ПЕРЕД `flush`/`fsync`/`close`; на APFS це проходило, на exFAT через
   fskit закриття перебивало мітку — файл отримував «зараз».
2. Дата СТВОРЕННЯ не відновлювалася ніде й ніколи. Для злиття між різними
   томами (де переносу не існує і копія — єдиний шлях) це означало втрату
   дати створення на кожному файлі.
3. План міг назвати унікальним те, що вже лежить у цілі байт-у-байт (так
   буває, коли знімок неповний: сторона A мала непрочитані шляхи). Копія
   тоді створювала дублікат із суфіксом ` (2)` — множила дані замість
   дедуплікації.

Інваріант, який ці правки НЕ послаблюють: ціль ніколи не перезаписується.
Пропуск дозволений РІВНО тоді, коли вміст цілі побайтово збігається з
джерелом; за іншого вмісту суфікс-retry лишається (див. останній тест).
"""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path  # noqa: E402

import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.fsops as fsops  # noqa: E402
from qa.fs_images import hdiutil_available, mount_image, unmount  # noqa: E402

OLD_MTIME_NS = 1_400_000_000_000_000_000  # 2014-05-13
OLD_CRTIME_NS = 1_300_000_000_000_000_000  # 2011-03-13


def _stale_plan(source: Path, target: Path, name: str):
    """Результат скану лише джерела + рукотворний рядок плану.

    Саме така пара «знімок без сторони A + план, що вважає файл унікальним»
    і привела до дублікатів ` (2)` на диску власника.
    """
    result = core.scan([str(source)])
    path = source / name
    return result, [(path.stat().st_size, str(path), name)]


def _birth_ns(path) -> int:
    st = os.stat(path)
    value = getattr(st, "st_birthtime_ns", None)
    return int(value) if value else int(getattr(st, "st_birthtime", 0) * 1e9)


# --- дата створення (працює на будь-якій ФС, тому без образа) --------------


def test_copy_preserves_creation_time(tmp_path):
    """Копія несе дату створення джерела, а не «зараз»."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "unique.bin"
    unique.write_bytes(b"unique-content")
    fsops._set_creation_time(str(unique), OLD_CRTIME_NS)
    assert abs(_birth_ns(unique) - OLD_CRTIME_NS) < 2_000_000_000, (
        "підготовка тесту не спрацювала: джерелу не виставилась дата створення")
    result, plan = _stale_plan(source, target, "unique.bin")

    copied, errors = fsops._copy_files(result, plan, str(target), str(source))

    assert errors == []
    assert copied
    assert abs(_birth_ns(target / "unique.bin") - OLD_CRTIME_NS) < 2_000_000_000


def test_copy_preserves_modification_time(tmp_path):
    """Копія несе дату зміни джерела точно, до наносекунди."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "unique.bin"
    unique.write_bytes(b"unique-content")
    os.utime(unique, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    result, plan = _stale_plan(source, target, "unique.bin")

    copied, errors = fsops._copy_files(result, plan, str(target), str(source))

    assert errors == []
    assert copied
    assert os.stat(target / "unique.bin").st_mtime_ns == OLD_MTIME_NS


def test_copy_reapplies_dates_after_publication(tmp_path, monkeypatch):
    """Мітки виставляються ПІСЛЯ публікації, а не до неї.

    Симуляція exFAT/fskit: публікація «псує» дату зміни, як це робить
    закриття файла на реальному томі. Якщо `utime` стоїть до публікації,
    цей тест падає — рівно так воно й було на диску власника.
    """
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "unique.bin"
    unique.write_bytes(b"unique-content")
    os.utime(unique, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
    real_publish = fsops._publish_no_overwrite

    def publish_then_touch(src_name, src_fd, dst_name, dst_fd):
        published = real_publish(src_name, src_fd, dst_name, dst_fd)
        if published:
            os.utime(dst_name, dir_fd=dst_fd, follow_symlinks=False)
        return published

    monkeypatch.setattr(fsops, "_publish_no_overwrite", publish_then_touch)
    result, plan = _stale_plan(source, target, "unique.bin")

    copied, errors = fsops._copy_files(result, plan, str(target), str(source))

    assert errors == []
    assert copied
    assert os.stat(target / "unique.bin").st_mtime_ns == OLD_MTIME_NS


# --- справжня унікальність -------------------------------------------------


def test_copy_skips_file_already_identical_in_destination(tmp_path):
    """Ціль уже має той самий вміст → нічого не копіюється, ` (2)` немає."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "same.bin").write_bytes(b"identical-bytes")
    (target / "same.bin").write_bytes(b"identical-bytes")
    result, plan = _stale_plan(source, target, "same.bin")
    skipped: list[tuple[str, str]] = []  # узгоджено з fsops._transfer_files

    copied, errors = fsops._copy_files(
        result, plan, str(target), str(source), skipped_out=skipped)

    assert errors == []
    assert copied == []
    # Разом зі шляхом пропуску йде ДОКАЗ — та копія в цілі, збіг з якою
    # щойно доведено повним читанням. Її потім приймає гейт Кошика.
    assert skipped == [(str(source / "same.bin"), str(target / "same.bin"))]
    assert not (target / "same (2).bin").exists()
    assert sorted(p.name for p in target.iterdir()) == ["same.bin"]
    assert (source / "same.bin").read_bytes() == b"identical-bytes"


def test_move_skips_file_already_identical_in_destination(tmp_path):
    """Те саме для переносу: джерело лишається, дублікат не плодиться.

    Файл лишається в джерелі свідомо — крок «теку в Кошик» потім знайде
    для нього живу копію в цілі, поза текою-джерелом, і пропустить теку.
    """
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "same.bin").write_bytes(b"identical-bytes")
    (target / "same.bin").write_bytes(b"identical-bytes")
    result, plan = _stale_plan(source, target, "same.bin")
    skipped: list[tuple[str, str]] = []  # узгоджено з fsops._transfer_files

    moved, errors = fsops._move_files(
        result, plan, str(target), str(source), skipped_out=skipped)

    assert errors == []
    assert moved == []
    assert skipped == [(str(source / "same.bin"), str(target / "same.bin"))]
    assert not (target / "same (2).bin").exists()
    assert (source / "same.bin").read_bytes() == b"identical-bytes"


def test_different_content_still_gets_suffix_and_never_overwrites(tmp_path):
    """Регресія на інваріант: інший вміст → суфікс, ціль недоторканна."""
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "same.bin").write_bytes(b"source-bytes")
    (target / "same.bin").write_bytes(b"target-bytes")
    result, plan = _stale_plan(source, target, "same.bin")

    copied, errors = fsops._copy_files(result, plan, str(target), str(source))

    assert errors == []
    assert [dst for _src, dst in copied] == [str(target / "same (2).bin")]
    assert (target / "same.bin").read_bytes() == b"target-bytes"
    assert (target / "same (2).bin").read_bytes() == b"source-bytes"


def test_skipped_uncovered_file_still_lets_source_folder_reach_trash(tmp_path):
    """Пропуск не має ламати крок «теку-джерело в Кошик».

    Тонке місце, на якому я спершу помилився. Гейт Кошика вимагає для
    КОЖНОГО лишкового файла доказ живої копії поза текою, а бере його з
    `res.file_class`. Файл у `__pycache__` (EXCLUDE_NAMES) скан не бачить
    ніколи, тож доказу в нього немає за побудовою.

    До пропуску такий файл ПЕРЕНОСИВСЯ в ціль (категорія «uncovered»), і
    джерело лишалось без нього. Пропуск лишає його на місці — і тека
    перестала проходити гейт, хоча ідентична копія доведено лежить у цілі.

    Доказ для неї є: `_destination_holds_same_content` прочитав ціль
    цілком і звірив BLAKE3. Його треба передати в гейт, а не викидати.
    """
    source, target = tmp_path / "source", tmp_path / "target"
    (source / "__pycache__").mkdir(parents=True)
    (target / "__pycache__").mkdir(parents=True)
    (source / "__pycache__" / "m.pyc").write_bytes(b"same-bytes" * 40)
    (target / "__pycache__" / "m.pyc").write_bytes(b"same-bytes" * 40)
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    assert plan, "неохоплений файл мусить потрапити в план"
    skipped: list = []

    moved, errors = fsops._move_files(
        result, plan, str(target), str(source), skipped_out=skipped)

    assert errors == []
    assert moved == []
    assert skipped, "ідентичний вміст у цілі мав дати пропуск"
    trashed: list[str] = []

    kind, payload = fsops._verify_then_trash_dir(
        result, str(source),
        trash=lambda paths, **_kw: (trashed.extend(paths), [])[1],
        extra_survivors=dict(skipped),
    )

    assert kind == "ok", f"тека мала пройти гейт Кошика, а не {kind}/{payload}"
    assert trashed == [str(source)]
    # Незалежна копія лишилась живою — саме вона й дозволила Кошик.
    assert (target / "__pycache__" / "m.pyc").read_bytes() == b"same-bytes" * 40


# --- те саме на РЕАЛЬНОМУ exFAT (там баг і виник) --------------------------


@pytest.mark.skipif(
    os.environ.get("DUPSCAN_FS_MATRIX") != "1" or not hdiutil_available(),
    reason="потрібен hdiutil: увімкнути DUPSCAN_FS_MATRIX=1",
)
def test_copy_preserves_both_dates_on_real_exfat():
    """Диск власника — exFAT. Обидві дати мусять доїхати саме там."""
    _image, mount_point = mount_image("ExFAT", size_mb=32)
    try:
        source, target = Path(mount_point) / "src", Path(mount_point) / "dst"
        source.mkdir()
        target.mkdir()
        unique = source / "unique.bin"
        unique.write_bytes(b"unique-content")
        os.utime(unique, ns=(OLD_MTIME_NS, OLD_MTIME_NS))
        fsops._set_creation_time(str(unique), OLD_CRTIME_NS)
        result, plan = _stale_plan(source, target, "unique.bin")

        copied, errors = fsops._copy_files(
            result, plan, str(target), str(source))

        assert errors == []
        assert copied
        destination = target / "unique.bin"
        assert os.stat(destination).st_mtime_ns == OLD_MTIME_NS
        assert abs(_birth_ns(destination) - OLD_CRTIME_NS) < 2_000_000_000
    finally:
        unmount(mount_point)
