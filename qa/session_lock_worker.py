"""Робочі процеси для конкурентного тесту сховища сесій.

Окремий модуль, а не всередині tests/: під multiprocessing(spawn)
дочірній процес re-import-ить цільову функцію за іменем модуля, а імена,
під якими pytest збирає тестові файли, не завжди передбачувані (та сама
причина, що й у qa/publish_race_worker.py).
"""

from __future__ import annotations

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "src"))


def _widen_listdir_race(delay: float) -> None:
    """Штучно розширює вікно TOCTOU навколо os.listdir у ЦЬОМУ процесі:
    без цього дві швидкі локальні операції (крихітний файл, SSD) рідко
    справді перетинаються — вікно природно закривається за мікросекунди,
    і тест був би недоказовим («пройшов» і без локу, і з локом, з
    однакових причин). Це стандартний прийом доведення TOCTOU: штучно
    розтягнути вікно між «прочитати список» і «видалити за списком», щоб
    гарантувати перетин, а не сподіватись на удачу планувальника ОС.
    Патчиться сам модуль os (спільний на процес) — короткоживучий
    dedicated-воркер, безпечно."""
    real_listdir = os.listdir

    def slow_listdir(path, *a, **k):
        names = real_listdir(path, *a, **k)
        time.sleep(delay)
        return names

    os.listdir = slow_listdir


def run_save_rounds(
    base_dir: str, tag: str, tmp_root: str, rounds: int, keep: int,
    barrier, queue, *, widen_race_window: float = 0.0,
) -> None:
    """Один учасник гонитви: `rounds` разів поспіль скан+save_session у
    СПІЛЬНИЙ base_dir, синхронізовано бар'єром на кожному раунді.

    `keep` підмінює ЗНАЧЕННЯ ЗА ЗАМОВЧУВАННЯМ параметра `_prune_old.keep`
    (через __defaults__), а не просто session._KEEP: default-аргумент
    Python звʼязується з об'єктом функції ОДИН РАЗ при визначенні модуля,
    тож `session._KEEP = keep` після імпорту нічого не змінює для
    `save_session`, який кличе `_prune_old(sdir, preserve_paths=...)` без
    явного `keep=`. Маленьке число робить prune реально активним на
    кожному збереженні цього процесу, а не раз на 30.
    """
    try:
        import dupscan.domain.core as core
        import dupscan.infra.session as session

        session._prune_old.__defaults__ = (
            keep, *session._prune_old.__defaults__[1:])
        if widen_race_window > 0:
            _widen_listdir_race(widen_race_window)
        saved: list[str] = []
        for round_id in range(rounds):
            tree = os.path.join(tmp_root, f"{tag}-{round_id}")
            os.makedirs(tree, exist_ok=True)
            payload = f"{tag}-{round_id}-".encode() + os.urandom(64)
            with open(os.path.join(tree, "f.bin"), "wb") as fh:
                fh.write(payload)
            result = core.scan([tree])
            barrier.wait(timeout=30)
            path = session.save_session(result, [tree], base_dir=base_dir)
            saved.append(path)
        queue.put((tag, saved, None))
    except Exception as exc:  # noqa: BLE001 — усе звітується батьку
        queue.put((tag, [], repr(exc)))


def run_import_rounds(
    base_dir: str, tag: str, tmp_root: str, rounds: int, keep: int,
    barrier, queue, *, widen_race_window: float = 0.0,
) -> None:
    """Третій учасник (B3, мікроблок H): `import_session` у СПІЛЬНИЙ
    base_dir, той самий бар'єр/цикл, що й `run_save_rounds` —
    import_session мав ТОЙ САМИЙ вразливий патерн (_write_meta ->
    _atomic_copy -> _prune_old), не обгорнутий тим самим локом, що
    закриває цей патерн скрізь, окрім тут. Джерело для імпорту — ОДИН валідний файл
    сесії поза сховищем (import не мутує src, повторне використання на
    кожному раунді безпечне й дешевше за новий скан щоразу)."""
    try:
        import dupscan.domain.core as core
        import dupscan.infra.session as session

        session._prune_old.__defaults__ = (
            keep, *session._prune_old.__defaults__[1:])
        if widen_race_window > 0:
            _widen_listdir_race(widen_race_window)
        seed_tree = os.path.join(tmp_root, f"{tag}-seed")
        os.makedirs(seed_tree, exist_ok=True)
        with open(os.path.join(seed_tree, "f.bin"), "wb") as fh:
            fh.write(f"{tag}-seed-".encode() + os.urandom(64))
        seed_result = core.scan([seed_tree])
        seed_saved = session.save_session(
            seed_result, [seed_tree],
            base_dir=os.path.join(tmp_root, f"{tag}-seed-store"))
        exported = os.path.join(tmp_root, f"{tag}-exported.json")
        session.export_session(seed_saved, exported)

        imported: list[str] = []
        for _round_id in range(rounds):
            barrier.wait(timeout=30)
            path = session.import_session(exported, base_dir=base_dir)
            imported.append(path)
        queue.put((tag, imported, None))
    except Exception as exc:  # noqa: BLE001 — усе звітується батьку
        queue.put((tag, [], repr(exc)))


def run_compact_rounds(
    base_dir: str, tag: str, rounds: int, max_bytes: int, barrier, queue,
    *, widen_race_window: float = 0.0,
) -> None:
    """Другий учасник (B2): compact_store (з малим max_bytes — реально
    щось прибирає щоразу) замість save_session, той самий бар'єр/цикл."""
    try:
        import dupscan.infra.session as session

        if widen_race_window > 0:
            _widen_listdir_race(widen_race_window)
        results = []
        for _round_id in range(rounds):
            barrier.wait(timeout=30)
            stats = session.compact_store(base_dir=base_dir, max_bytes=max_bytes)
            results.append(stats)
        queue.put((tag, results, None))
    except Exception as exc:  # noqa: BLE001
        queue.put((tag, [], repr(exc)))
