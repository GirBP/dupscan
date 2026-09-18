"""E2E: репліка структури власника для тесту-двійника.

Власник зливає дві теки на зовнішньому диску (BARRACUDA): "downloads"
(ціль) і "downloads copy" (джерело, з пробілом в імені — так, як
Finder/macOS сам називає копію). Ця теки-двійник відтворює всі елементи,
які реально спричиняли відмови гейта пари:

- службові теки (__pycache__, .git) глибоко в дереві джерела — навмисне
  виключення обходу, не збій читання (core.dir_read_failed);
- кириличні імена файлів і тек (у Finder-копіях це типово);
- 3+ пари справжніх дублікатів (≥64 КіБ, випадковий вміст) — лишаються на
  місці при злитті, дають докір теки-джерела в Кошик;
- унікальні файли з обох боків — переносяться (copy→downloads) або
  лишаються (downloads);
- symlink і hardlink-пара — не мають ламати Merkle-манифест ні
  сканування, ні злиття;
- файл 0 байтів — окрема воронка в core.scan (zero_key);
- колізія імені: той самий відносний шлях існує в обох теках, але з
  РІЗНИМ вмістом — при переносі має отримати суфікс " (2)", а не
  перезаписати ціль.

build_owner_tree(base) створює дерево і повертає словник шляхів, яким
користується tests/test_owner_scenario.py.
"""

from __future__ import annotations

import os
from pathlib import Path


def _make(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_owner_tree(base: Path) -> dict:
    base = Path(base)
    downloads = base / "downloads"
    copy = base / "downloads copy"  # пробіл в імені — точна назва Finder-копії

    paths: dict = {
        "base": base,
        "downloads": downloads,
        "copy": copy,
    }

    # ---- 1. Справжні дублікати (≥64 КіБ, os.urandom), лишаються на місці --
    duplicate_rels: list[str] = []
    for index in range(3):
        rel = f"d/дублікат_{index}.bin"
        data = os.urandom(64 * 1024 + index * 97)  # трохи різні розміри, усі ≥64 КіБ
        _make(copy / rel, data)
        _make(downloads / rel, data)
        duplicate_rels.append(rel)
    paths["duplicate_rels"] = duplicate_rels

    # ---- 2. Службові теки глибоко в джерелі: EXCLUDE_NAMES ----------------
    pycache_rel = "d/все/графіка/Lab 2/__pycache__/controls.cpython-311.pyc"
    _make(copy / pycache_rel, os.urandom(2048))
    paths["pycache_rel"] = pycache_rel
    paths["pycache_path"] = copy / pycache_rel

    git_rel = ".git/config"
    _make(copy / git_rel, b"[core]\n\trepositoryformatversion = 0\n")
    paths["git_rel"] = git_rel
    paths["git_path"] = copy / git_rel

    # ---- 3. Кириличні імена -----------------------------------------------
    cyrillic_copy_rel = "d/все/графіка/Lab 2/дипломна_робота.docx"
    _make(copy / cyrillic_copy_rel, os.urandom(9000))
    paths["cyrillic_copy_rel"] = cyrillic_copy_rel
    paths["cyrillic_copy_path"] = copy / cyrillic_copy_rel

    cyrillic_downloads_rel = "архів/звіт.pdf"
    _make(downloads / cyrillic_downloads_rel, os.urandom(9000))
    paths["cyrillic_downloads_rel"] = cyrillic_downloads_rel
    paths["cyrillic_downloads_path"] = downloads / cyrillic_downloads_rel

    # ---- 4. Унікальні файли з обох боків (не кириличні, для контрасту) ---
    unique_copy_rel = "тільки_у_копії.bin"
    _make(copy / unique_copy_rel, os.urandom(5000))
    paths["unique_copy_rel"] = unique_copy_rel
    paths["unique_copy_path"] = copy / unique_copy_rel

    unique_downloads_rel = "тільки_у_downloads.bin"
    _make(downloads / unique_downloads_rel, os.urandom(5000))
    paths["unique_downloads_rel"] = unique_downloads_rel
    paths["unique_downloads_path"] = downloads / unique_downloads_rel

    # ---- 5. Symlink усередині джерела -------------------------------------
    symlink_rel = "d/лінк_на_файл"
    link_path = copy / symlink_rel
    link_path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(
        os.path.relpath(copy / unique_copy_rel, link_path.parent), link_path)
    paths["symlink_rel"] = symlink_rel
    paths["symlink_path"] = link_path

    # ---- 6. Hardlink-пара усередині ОДНІЄЇ теки (джерела) -----------------
    # На exFAT/FAT/SMB жорстких посилань НЕМАЄ (os.link → ENOTSUP 45) — а саме
    # exFAT стоїть на диску власника. Дерево-двійник мусить будуватись і там,
    # інакше матриця ФС не зможе прогнати на ньому ланцюг злиття. Без посилань
    # пара лишається парою однакового вмісту — на такій ФС це чесний дублікат,
    # рівно те, що власник бачить у себе. Прапорець каже правду про ФС, щоб
    # тест звіряв inode лише там, де він взагалі має сенс.
    hardlink_a_rel = "hardlink_a.bin"
    hardlink_b_rel = "hardlink_b.bin"
    hardlink_a_path = copy / hardlink_a_rel
    hardlink_b_path = copy / hardlink_b_rel
    hardlink_payload = os.urandom(6000)
    _make(hardlink_a_path, hardlink_payload)
    try:
        os.link(hardlink_a_path, hardlink_b_path)
    except OSError:
        _make(hardlink_b_path, hardlink_payload)
        hardlinks_supported = False
    else:
        hardlinks_supported = True
    paths["hardlink_a_rel"] = hardlink_a_rel
    paths["hardlink_b_rel"] = hardlink_b_rel
    paths["hardlink_a_path"] = hardlink_a_path
    paths["hardlink_b_path"] = hardlink_b_path
    paths["hardlinks_supported"] = hardlinks_supported

    # ---- 7. Файл 0 байтів (окрема воронка в core.scan) --------------------
    zero_byte_rel = "порожній.txt"
    _make(copy / zero_byte_rel, b"")
    paths["zero_byte_rel"] = zero_byte_rel
    paths["zero_byte_path"] = copy / zero_byte_rel

    # ---- 8. Колізія імені у цілі: той самий rel-шлях, ІНШИЙ вміст ---------
    collision_rel = "нотатки.txt"
    _make(downloads / collision_rel, b"downloads-version-of-the-file\n" * 50)
    _make(copy / collision_rel, b"COPY-VERSION-DIFFERENT-CONTENT\n" * 50)
    paths["collision_rel"] = collision_rel
    paths["collision_downloads_path"] = downloads / collision_rel
    paths["collision_copy_path"] = copy / collision_rel

    return paths
