"""Реальні файлові системи для тестів: створити образ, змонтувати, прибрати.

Спільне для tests/test_fs_matrix.py (сканування) і
tests/test_fs_matrix_merge.py (ланцюг злиття), щоб дві матриці не розійшлися
у тому, ЯК монтується том.

Пастка, що коштувала прогону: мітка тому exFAT — максимум **11 символів**.
При довшій `hdiutil create -fs ExFAT` відмовляє повідомленням
`Operation not permitted`, яке виглядає як заборона пісочниці й посилає
шукати проблему не там. Тому мітка тут коротка ЗАВЖДИ, для всіх ФС.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid

# Мітка тому: 2 + 6 = 8 символів, з запасом до межі exFAT в 11.
_LABEL_PREFIX = "ds"


def hdiutil_available() -> bool:
    """hdiutil є не в кожній пісочниці — там матриця пропускається."""
    return shutil.which("hdiutil") is not None


def mount_image(fs: str, size_mb: int = 32) -> tuple[str, str]:
    """Створити образ файлової системи `fs` і змонтувати його.

    Повертає (шлях_до_образа, точка_монтування).
    """
    label = f"{_LABEL_PREFIX}{uuid.uuid4().hex[:6]}"
    assert len(label) <= 11, "мітка тому не влізе в межу exFAT"
    image = os.path.join(tempfile.mkdtemp(), f"{label}.dmg")
    subprocess.run(
        ["hdiutil", "create", "-size", f"{size_mb}m", "-fs", fs,
         "-volname", label, image],
        check=True,
        capture_output=True,
    )
    attached = subprocess.run(
        ["hdiutil", "attach", image, "-nobrowse"],
        check=True, capture_output=True, text=True,
    ).stdout
    mount_point = attached.strip().splitlines()[-1].split("\t")[-1].strip()
    return image, mount_point


def unmount(mount_point: str) -> None:
    subprocess.run(["hdiutil", "detach", mount_point, "-force"], capture_output=True)
