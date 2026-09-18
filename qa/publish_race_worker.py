"""Робочий процес для конкурентного тесту fsops._publish_no_overwrite.

Окремий модуль, а не всередині tests/test_publish_race.py: під
multiprocessing(spawn) дочірній процес re-import-ить цільову функцію за
іменем модуля, а pytest збирає тестові файли не завжди під передбачуваним
importlib-іменем. qa.* — звичайний, стабільно імпортовний пакет (як
qa.fs_images, яким уже користується tests/test_fs_matrix_merge.py).
"""

from __future__ import annotations

import os
import sys

# Дочірній spawn-процес успадковує sys.path батька (multiprocessing кладе
# його в preparation data), але модуль лишається самодостатнім і тут —
# про всяк випадок, якщо колись викликається інакше, ніж через тест.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))


def run_publish_round(dirpath: str, tag: str, payload: bytes,
                       dest_name: str, barrier, queue) -> None:
    """Один учасник гонитви: записати ПРИВАТНЕ джерело, дочекатись бар'єру
    старту з іншим учасником, опублікувати під dest_name (з одним
    фолбеком-суфіксом — рівно стільки, скільки учасників гонитви), і
    звітувати (tag, опубліковане_ім'я_або_None, помилка_або_None).

    Жодна помилка не губиться мовчки: усе, що може статися (включно з
    BrokenBarrierError), ловиться і йде в queue — інакше зависла дитина
    перетворює падіння тесту на непрозорий таймаут join().
    """
    try:
        import dupscan.infra.fsops as fsops  # локально: дочірній процес ще не імпортував модуль

        source_name = f".src-{tag}"
        source_path = os.path.join(dirpath, source_name)
        with open(source_path, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        dir_fd = os.open(dirpath, os.O_RDONLY)
        try:
            barrier.wait(timeout=30)
            base, ext = os.path.splitext(dest_name)
            candidates = [dest_name, f"{base} (2){ext}"]
            published = None
            for candidate in candidates:
                if fsops._publish_no_overwrite(
                        source_name, dir_fd, candidate, dir_fd):
                    published = candidate
                    break
            queue.put((tag, published, None))
        finally:
            os.close(dir_fd)
    except Exception as exc:  # noqa: BLE001 — усе звітується батьку, не губиться
        queue.put((tag, None, repr(exc)))
