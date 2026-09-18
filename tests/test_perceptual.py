"""Перцептивна подібність зображень —
алгоритм і воронка (не GUI/деструктив — те в test_perceptual_readonly_
invariant.py і test_perceptual_tab_ui.py).

Адаптовано з DupFinder tests/test_imagehash.py + tests/test_perceptual_
scanner.py: хеш переписано на QImage (PySide6, без Pillow — продукт прямо
вимагає нуль нових runtime-залежностей), геометрія dHash/union-find та
сама.
"""

import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.perceptual as perceptual  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _make_gradient_image(path: str, seed: int, size: int = 64) -> None:
    img = QImage(size, size, QImage.Format.Format_RGB32)
    for y in range(size):
        for x in range(size):
            v = (x * 4 + y * 4 + seed) % 256
            img.setPixelColor(x, y, QColor(v, v, v))
    img.save(path, "PNG")


# --------------------------------------------------------------------------- #
# dhash_from_gray — чисто, без QImage
# --------------------------------------------------------------------------- #
def test_dhash_from_gray_ascending_row_is_all_zero_bits():
    pixels = [0, 1, 2, 3, 4, 5, 6, 7, 8]  # width=9, height=1: усі left<right
    assert perceptual.dhash_from_gray(pixels, 9, 1) == 0


def test_dhash_from_gray_descending_row_is_all_one_bits():
    pixels = [8, 7, 6, 5, 4, 3, 2, 1, 0]
    assert perceptual.dhash_from_gray(pixels, 9, 1) == 0b11111111


def test_hamming_distance_basic():
    assert perceptual.hamming(0b0000, 0b0000) == 0
    assert perceptual.hamming(0b0000, 0b1111) == 4
    assert perceptual.hamming(0b1010, 0b0101) == 4


# --------------------------------------------------------------------------- #
# perceptual_hash (QImage) — та сама картинка, resize/reencode -> одна група
# --------------------------------------------------------------------------- #
def test_resized_reencoded_copy_hashes_close(tmp_path):
    """resize/переконвертація тієї самої картинки -> одна група (мала
    відстань Геммінга)."""
    original = str(tmp_path / "a.png")
    _make_gradient_image(original, seed=0)
    resized = str(tmp_path / "a_small.jpg")
    img = QImage(original)
    img.scaled(24, 24).save(resized, "JPEG", quality=80)

    h1 = perceptual.perceptual_hash(original)
    h2 = perceptual.perceptual_hash(resized)
    assert h1 is not None and h2 is not None
    assert perceptual.hamming(h1, h2) <= perceptual.DEFAULT_MAX_DISTANCE


def test_different_images_hash_far_apart(tmp_path):
    a = str(tmp_path / "a.png")
    b = str(tmp_path / "b.png")
    _make_gradient_image(a, seed=0)
    _make_gradient_image(b, seed=140)

    h1 = perceptual.perceptual_hash(a)
    h2 = perceptual.perceptual_hash(b)
    assert h1 is not None and h2 is not None
    assert perceptual.hamming(h1, h2) > perceptual.DEFAULT_MAX_DISTANCE


def test_perceptual_hash_none_for_missing_file(tmp_path):
    assert perceptual.perceptual_hash(str(tmp_path / "nope.png")) is None


def test_perceptual_hash_none_for_non_image(tmp_path):
    text = tmp_path / "notes.txt"
    text.write_text("не зображення")
    assert perceptual.perceptual_hash(str(text)) is None


# --------------------------------------------------------------------------- #
# group_similar — транзитивність, поріг
# --------------------------------------------------------------------------- #
def test_group_similar_is_transitive_and_ignores_singletons():
    hashes = {
        "/a.png": 0b0000_0000,
        "/b.png": 0b0000_0001,  # близько до a (1 біт)
        "/c.png": 0b1111_1111,  # далеко від усіх
    }
    groups = perceptual.group_similar(hashes, max_distance=2)
    assert groups == [["/a.png", "/b.png"]]  # /c.png — синглтон, не група


# --------------------------------------------------------------------------- #
# обхід — не-зображення ігноруються, dataless не читається
# --------------------------------------------------------------------------- #
def test_non_images_are_ignored_by_walk(tmp_path):
    (tmp_path / "photo.png").write_bytes(b"fake-but-has-image-suffix")
    (tmp_path / "notes.txt").write_bytes("текст, не фото".encode())
    (tmp_path / "archive.zip").write_bytes(b"binary")
    cancel = threading.Event()
    found = perceptual._walk_images([str(tmp_path)], cancel=cancel)
    assert found == [str(tmp_path / "photo.png")]


def test_dataless_icloud_file_is_never_read(tmp_path, monkeypatch):
    """iCloud-файл без локального вмісту: читання = тихе викачування —
    воронка НІКОЛИ його не чіпає (SF_DATALESS)."""
    placeholder = tmp_path / "cloud.jpg"
    placeholder.write_bytes(b"")

    real_lstat = os.lstat

    class _FakeStat:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        @property
        def st_flags(self):
            return 0x40000000  # SF_DATALESS

        @property
        def st_mode(self):
            return self._real.st_mode

    def fake_lstat(path, *a, **kw):
        real = real_lstat(path, *a, **kw)
        if os.path.normpath(str(path)) == os.path.normpath(str(placeholder)):
            return _FakeStat(real)
        return real

    monkeypatch.setattr(os, "lstat", fake_lstat)
    cancel = threading.Event()
    errors: list[str] = []
    found = perceptual._walk_images(
        [str(tmp_path)], cancel=cancel, errors=errors)
    assert found == []
    assert any("iCloud" in e for e in errors)


def test_symlinked_file_is_not_followed(tmp_path):
    real = tmp_path / "real.png"
    _make_gradient_image(str(real), seed=0)
    link = tmp_path / "link.png"
    try:
        os.symlink(real, link)
    except OSError:
        return  # платформа без прав на symlink — пропустити тест чесно
    cancel = threading.Event()
    found = perceptual._walk_images([str(tmp_path)], cancel=cancel)
    assert found == [str(real)]


def test_cancel_stops_the_walk_promptly(tmp_path):
    for i in range(50):
        sub = tmp_path / f"d{i}"
        sub.mkdir()
        _make_gradient_image(str(sub / "p.png"), seed=i)
    cancel = threading.Event()
    cancel.set()  # скасовано ДО старту
    found = perceptual._walk_images([str(tmp_path)], cancel=cancel)
    assert found == []


def test_find_similar_images_groups_end_to_end(tmp_path):
    """Повна воронка: дві схожі картинки в різних теках -> одна група."""
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    _make_gradient_image(str(a_dir / "photo.png"), seed=0)
    img = QImage(str(a_dir / "photo.png"))
    img.scaled(20, 20).save(str(b_dir / "photo_small.jpg"), "JPEG")

    result = perceptual.find_similar_images([str(tmp_path)])
    assert result.images_hashed == 2
    assert result.total_groups == 1
    assert result.groups[0].count == 2
