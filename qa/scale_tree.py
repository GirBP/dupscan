"""Синтетичне дерево для стенда масштабу.

build_scale_tree(root, n_files, dup_ratio, fanout) швидко матеріалізує
n_files малих файлів на диску під root, збалансовану глибину піддиректорій
(branching factor fanout — і піддиректорій на рівень, і файлів на кінцеву
теку: єдиний параметр керує обома, четвертого параметра в сигнатурі спецом
не передбачено), і контрольовану частку точних дублікатів.

Вміст детермінований ЗА ІНДЕКСОМ файла/пари — жодного random чи Date.now
(в цьому оточенні вони заборонені, і для гейта регресій вони й шкідливі:
два прогони з тим самим (n_files, dup_ratio, fanout) мусять дати ПОБАЙТОВО
те саме дерево, інакше "той самий склад груп" між прогонами неможливо
довести — test_scale_tree_generator_is_deterministic саме це й перевіряє).

Схема вмісту (_payload): заголовок = префікс ("D" для дубліката, "U" для
унікального) + 8-байтовий big-endian індекс. Заголовок ін'єктивний за
(префікс, індекс) -> два файли з РІЗНИХ пар/унікальних індексів ніколи не
збігаються вмістом випадково; будь-яка збіжність у file_groups після
core.scan доводить саме те, що генератор ЗАДУМАВ, а не випадковий шум.
Розмір файла (16..79 Б) залежить від індексу за модулем 64 — навмисно
малий, щоб 100k/300k файлів займали одиниці-десятки МіБ на диску, і
дозволяє більшості файлів звалитись в різні size-бакети (core.scan спершу
групує за розміром — синглтон розміру одразу стає унікальним без жодного
читання вмісту), а не лише в один гігантський бакет.
"""

from __future__ import annotations

import os

# Мінімальний розмір файла (Б) — навмисно кілька байтів, не порожній файл.
_BASE_SIZE = 16
# Розмір варіюється за індексом у цьому діапазоні: розкидає файли по
# size-бакетах core.scan (а не в один суцільний бакет розміру _BASE_SIZE).
_SIZE_SPREAD = 64


def _payload(prefix: bytes, index: int, length: int) -> bytes:
    """Детермінований вміст довжини length, ін'єктивний за (prefix, index).

    header = prefix + 8-байтовий big-endian index — 9 Б, унікальні для
    кожної (prefix, index) пари. length завжди >= _BASE_SIZE = 16 > 9, тож
    header ніколи не обрізається; filler лише добиває довжину, повторної
    унікальності від нього не вимагається.
    """
    header = prefix + index.to_bytes(8, "big")
    if length <= len(header):
        return header[:length]
    filler = bytes((index + i) % 256 for i in range(length - len(header)))
    return header + filler


def _leaf_dir(root: str, leaf_index: int, fanout: int, depth: int) -> str:
    """Шлях кінцевої теки для leaf_index: leaf_index у системі числення з
    основою fanout, депозицифровано в depth сегментів — кожна тека на
    кожному рівні має щонайбільше fanout дітей (fanout піддиректорій)."""
    digits = []
    x = leaf_index
    for _ in range(depth):
        digits.append(x % fanout)
        x //= fanout
    digits.reverse()
    return os.path.join(root, *(f"d{d}" for d in digits))


def build_scale_tree(root: str, n_files: int, dup_ratio: float, fanout: int) -> dict[str, int]:
    """Матеріалізувати синтетичне дерево з n_files файлів під root.

    dup_ratio — цільова частка файлів у парах точних дублікатів (пари по
    2, той самий вміст ⇒ той самий клас у core.scan); решта — унікальні,
    кожен зі своїм ін'єктивним вмістом. fanout — і кількість піддиректорій
    на рівень дерева, і файлів на кінцеву теку (збалансована глибина).

    Повертає лічильники: n_files (фактично створено — може бути на 1
    менше за запит, якщо dup-частка округлюється до парного числа),
    n_dup_pairs, n_unique_files, expected_groups (= n_dup_pairs — рівно
    стільки груп по 2 файли має дати core.scan після агрегації).
    """
    fanout = max(2, fanout)
    files_per_leaf = fanout
    n_leaves = max(1, -(-n_files // files_per_leaf))  # ceil-ділення
    depth = 1
    capacity = fanout
    while capacity < n_leaves:
        depth += 1
        capacity *= fanout

    n_dup_files = int(n_files * dup_ratio)
    n_dup_files -= n_dup_files % 2  # цілі пари
    n_dup_pairs = n_dup_files // 2

    os.makedirs(root, exist_ok=True)
    created_dirs: set[str] = set()

    def ensure_dir(path: str) -> None:
        if path not in created_dirs:
            os.makedirs(path, exist_ok=True)
            created_dirs.add(path)

    file_index = 0
    for leaf in range(n_leaves):
        if file_index >= n_files:
            break
        dirpath = _leaf_dir(root, leaf, fanout, depth)
        ensure_dir(dirpath)
        for slot in range(files_per_leaf):
            if file_index >= n_files:
                break
            if file_index < n_dup_pairs * 2:
                pair = file_index // 2
                size = _BASE_SIZE + (pair % _SIZE_SPREAD)
                data = _payload(b"D", pair, size)
            else:
                uniq_i = file_index - n_dup_pairs * 2
                size = _BASE_SIZE + (uniq_i % _SIZE_SPREAD)
                data = _payload(b"U", uniq_i, size)
            fp = os.path.join(dirpath, f"f{slot}.bin")
            with open(fp, "wb") as fh:
                fh.write(data)
            file_index += 1

    return {
        "n_files": file_index,
        "n_dup_pairs": n_dup_pairs,
        "n_unique_files": file_index - n_dup_pairs * 2,
        "expected_groups": n_dup_pairs,
    }
