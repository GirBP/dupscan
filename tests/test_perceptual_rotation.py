"""Перцептив v2: обертання і дзеркало.

czkawka групує обернені копії фото, DupScan 2.22.0 — ні: dHash рахувався
на сітці однієї орієнтації, тож поворот на 90° давав хеш на відстані
далеко за порогом. Канонічний хеш = мінімум по 8 діедральних варіантах
однієї квадратної сітки — групування (union-find по Геммінгу) не
змінюється взагалі.

Патерн зображень тут НАВМИСНО асиметричний (x*7 + y*3): помічений у
наявних тестах x*4+y*4 інваріантний під транспонуванням, і тест обертань
на ньому пройшов би навіть без фікса.
"""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtGui import QColor, QImage, QTransform  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import dupscan.ui.perceptual as perceptual  # noqa: E402

_qapp = QApplication.instance() or QApplication([])


def _asymmetric_image(path: str, seed: int, size: int = 48) -> QImage:
    img = QImage(size, size, QImage.Format.Format_RGB32)
    for y in range(size):
        for x in range(size):
            v = (x * 7 + y * 3 + seed) % 256
            img.setPixelColor(x, y, QColor(v, v, v))
    img.save(path, "PNG")
    return img


def _saved_transform(img: QImage, path: str, *, angle: int = 0,
                     mirror: bool = False) -> None:
    transform = QTransform().rotate(angle)
    if mirror:
        transform = transform.scale(-1, 1)
    img.transformed(transform).save(path, "PNG")


def test_rotated_copies_hash_within_group_distance(tmp_path):
    original = str(tmp_path / "orig.png")
    img = _asymmetric_image(original, seed=11)
    base = perceptual.perceptual_hash(original)
    assert base is not None
    for angle in (90, 180, 270):
        rotated = str(tmp_path / f"rot{angle}.png")
        _saved_transform(img, rotated, angle=angle)
        got = perceptual.perceptual_hash(rotated)
        assert got is not None
        assert perceptual.hamming(base, got) <= perceptual.DEFAULT_MAX_DISTANCE, (
            f"поворот {angle}°: відстань "
            f"{perceptual.hamming(base, got)} > порога — копію не згрупує")


def test_mirrored_copy_hashes_within_group_distance(tmp_path):
    original = str(tmp_path / "orig.png")
    img = _asymmetric_image(original, seed=23)
    base = perceptual.perceptual_hash(original)
    assert base is not None
    mirrored = str(tmp_path / "mirror.png")
    _saved_transform(img, mirrored, mirror=True)
    got = perceptual.perceptual_hash(mirrored)
    assert got is not None
    assert perceptual.hamming(base, got) <= perceptual.DEFAULT_MAX_DISTANCE


def test_distinct_images_stay_apart_after_canonicalization(tmp_path):
    """Мінімум по 8 варіантах звужує простір — перевірити, що різні
    зображення не злипаються (плата за інваріантність не з'їла точність)."""
    a_path, b_path = str(tmp_path / "a.png"), str(tmp_path / "b.png")
    _asymmetric_image(a_path, seed=5)
    b = QImage(48, 48, QImage.Format.Format_RGB32)
    for y in range(48):
        for x in range(48):
            v = (x * x * 3 + y * 13) % 256  # інша структура градієнта
            b.setPixelColor(x, y, QColor(v, v, v))
    b.save(b_path, "PNG")
    ha, hb = perceptual.perceptual_hash(a_path), perceptual.perceptual_hash(b_path)
    assert ha is not None and hb is not None
    assert perceptual.hamming(ha, hb) > perceptual.DEFAULT_MAX_DISTANCE


def test_rotated_file_lands_in_same_perceptual_group(tmp_path):
    """Наскрізний: find_similar_images кладе оригінал і повернуту копію
    в одну групу."""
    original = str(tmp_path / "photo.png")
    img = _asymmetric_image(original, seed=42)
    _saved_transform(img, str(tmp_path / "photo_rot90.png"), angle=90)
    result = perceptual.find_similar_images([str(tmp_path)])
    grouped = [set(g.files) for g in result.groups]
    assert any(
        {original, str(tmp_path / "photo_rot90.png")} <= g for g in grouped
    ), f"груп: {grouped}"
