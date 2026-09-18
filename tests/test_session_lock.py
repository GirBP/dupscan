"""Два інстанси DupScan і сховище сесій.

Загроза: конкурентний `_prune_old` (усередині `save_session`) і
паралельний `save_session`/`compact_store` можуть зіпсувати сховище —
prune одного процесу бачить ЧАСТКОВИЙ стан сусіда (застарілий
`os.listdir()`), тому може або знести щойно написану пару, або дати
торн-пару (payload без meta чи навпаки). Історія (`removal_history.py`)
вже має fcntl-лок навколо запису; тут той самий підхід для сесій.

Два процеси (multiprocessing, spawn), спільний sessions-dir, бар'єр на
кожному раунді — щоб save/save (B1) і save/compact (B2) стартували
якомога одночасніше. `session._KEEP` виставляється малим У КОЖНОМУ
процесі окремо (значення те саме, просто конфігурація локального
інтерпретера) — інакше prune не спрацьовує майже ніколи в маленькому
тесті (типовий keep=30).
"""

from __future__ import annotations

import multiprocessing
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402
from qa.session_lock_worker import (  # noqa: E402
    run_compact_rounds, run_import_rounds, run_save_rounds,
)

JOIN_TIMEOUT = 60
ROUNDS = 10
KEEP = 2
RACE_WINDOW = 0.05  # штучне розширення TOCTOU-вікна навколо os.listdir


def _payloads_with_intact_sidecars(sdir: str) -> list[str]:
    """Список payload-імен; асертує по дорозі, що КОЖЕН має сайдкар і що
    немає сайдкарів-сиріт без payload-пари (torn pair -> AssertionError
    з точним іменем). Використовує session._meta_path — те саме джерело
    істини щодо іменування, яким користуються save_session/_prune_old."""
    try:
        names = os.listdir(sdir)
    except OSError:
        return []
    payloads = sorted(n for n in names if session._is_payload(n))
    metas = {n for n in names if n.endswith(".meta.json")}
    for name in payloads:
        expected_meta = os.path.basename(
            session._meta_path(os.path.join(sdir, name)))
        assert expected_meta in metas, f"payload без сайдкара: {name}"
        metas.discard(expected_meta)
    assert not metas, f"сайдкар(и)-сироти без payload: {sorted(metas)}"
    return payloads


# ================================ B1 ========================================


def _run_b1_once(tmp_path_factory) -> None:
    tmp_path = tmp_path_factory.mktemp("b1")
    base_dir = str(tmp_path / "data")
    tmp_root = str(tmp_path / "trees")
    os.makedirs(tmp_root, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    kwargs = {"widen_race_window": RACE_WINDOW}
    procs = [
        ctx.Process(
            target=run_save_rounds,
            args=(base_dir, "A", tmp_root, ROUNDS, KEEP, barrier, queue),
            kwargs=kwargs),
        ctx.Process(
            target=run_save_rounds,
            args=(base_dir, "B", tmp_root, ROUNDS, KEEP, barrier, queue),
            kwargs=kwargs),
    ]
    for p in procs:
        p.start()

    results = {}
    for _ in range(2):
        tag, saved, error = queue.get(timeout=JOIN_TIMEOUT)
        results[tag] = (saved, error)
    for p in procs:
        p.join(timeout=JOIN_TIMEOUT)
        assert p.exitcode == 0, f"воркер {p.name} завершився з exitcode={p.exitcode}"

    assert results["A"][1] is None, f"процес A повідомив помилку: {results['A'][1]}"
    assert results["B"][1] is None, f"процес B повідомив помилку: {results['B'][1]}"

    sdir = os.path.join(base_dir, "sessions")
    payloads = _payloads_with_intact_sidecars(sdir)

    for name in payloads:
        path = os.path.join(sdir, name)
        loaded = session.load_session(path)  # МАЄ читатися без винятку
        assert loaded is not None

    last_a, last_b = results["A"][0][-1], results["B"][0][-1]
    assert last_a and os.path.exists(last_a), (
        "останній збережений раунд A мав пережити prune")
    assert last_b and os.path.exists(last_b), (
        "останній збережений раунд B мав пережити prune")
    assert len(payloads) == KEEP, (
        f"очікувано рівно keep={KEEP} пар після серіалізованого prune, "
        f"отримано {len(payloads)}: {sorted(payloads)}")


@pytest.mark.parametrize("attempt", range(3))
def test_b1_concurrent_save_session_leaves_no_torn_pairs(
        tmp_path_factory, attempt):
    """3 прогони поспіль (акцептанс ТЗ) — кожен незалежний, власний tmp_path."""
    _run_b1_once(tmp_path_factory)


# ================================ B2 ========================================


def _run_b2_once(tmp_path_factory) -> None:
    tmp_path = tmp_path_factory.mktemp("b2")
    base_dir = str(tmp_path / "data")
    tmp_root = str(tmp_path / "trees")
    os.makedirs(tmp_root, exist_ok=True)
    sdir = os.path.join(base_dir, "sessions")

    # Стартовий пул: щоб compact_store(max_bytes=...) мав що прибирати
    # одразу, а не ганявся за порожньою текою.
    os.makedirs(sdir, exist_ok=True)
    for i in range(4):
        tree = os.path.join(tmp_root, f"seed-{i}")
        os.makedirs(tree, exist_ok=True)
        with open(os.path.join(tree, "f.bin"), "wb") as fh:
            fh.write(f"seed-{i}-".encode() + os.urandom(256))
        session.save_session(core.scan([tree]), [tree], base_dir=base_dir)

    ctx = multiprocessing.get_context("spawn")
    rounds = 5
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=run_save_rounds,
            args=(base_dir, "saver", tmp_root, rounds, KEEP, barrier, queue),
            kwargs={"widen_race_window": RACE_WINDOW}),
        ctx.Process(
            target=run_compact_rounds,
            args=(base_dir, "compactor", rounds, 4096, barrier, queue),
            kwargs={"widen_race_window": RACE_WINDOW}),
    ]
    for p in procs:
        p.start()

    results = {}
    for _ in range(2):
        tag, payload, error = queue.get(timeout=JOIN_TIMEOUT)
        results[tag] = (payload, error)
    for p in procs:
        p.join(timeout=JOIN_TIMEOUT)
        assert p.exitcode == 0, f"воркер {p.name} завершився з exitcode={p.exitcode}"

    assert results["saver"][1] is None, results["saver"][1]
    assert results["compactor"][1] is None, results["compactor"][1]

    payloads = _payloads_with_intact_sidecars(sdir)
    for name in payloads:
        loaded = session.load_session(os.path.join(sdir, name))
        assert loaded is not None

    last_saved = results["saver"][0][-1]
    assert last_saved and os.path.exists(last_saved), (
        "останній save мав пережити паралельний compact_store")


@pytest.mark.parametrize("attempt", range(3))
def test_b2_compact_store_does_not_interleave_with_concurrent_save(
        tmp_path_factory, attempt):
    """3 прогони поспіль (акцептанс ТЗ), менший цикл ніж B1."""
    _run_b2_once(tmp_path_factory)


# ---- import_session || save_session: гонитва без _locked -------------------
# import_session (session.py) кличе _prune_old без _locked — той самий
# вразливий патерн гонитви, що й save_session. Тест ганяє import_session
# паралельно з save_session, перевіряючи саме цю гонитву.


def _run_b3_once(tmp_path_factory) -> None:
    tmp_path = tmp_path_factory.mktemp("b3")
    base_dir = str(tmp_path / "data")
    tmp_root = str(tmp_path / "trees")
    os.makedirs(tmp_root, exist_ok=True)

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    kwargs = {"widen_race_window": RACE_WINDOW}
    procs = [
        ctx.Process(
            target=run_import_rounds,
            args=(base_dir, "importer", tmp_root, ROUNDS, KEEP, barrier, queue),
            kwargs=kwargs),
        ctx.Process(
            target=run_save_rounds,
            args=(base_dir, "saver", tmp_root, ROUNDS, KEEP, barrier, queue),
            kwargs=kwargs),
    ]
    for p in procs:
        p.start()

    results = {}
    for _ in range(2):
        tag, produced, error = queue.get(timeout=JOIN_TIMEOUT)
        results[tag] = (produced, error)
    for p in procs:
        p.join(timeout=JOIN_TIMEOUT)
        assert p.exitcode == 0, f"воркер {p.name} завершився з exitcode={p.exitcode}"

    assert results["importer"][1] is None, (
        f"процес importer повідомив помилку: {results['importer'][1]}")
    assert results["saver"][1] is None, (
        f"процес saver повідомив помилку: {results['saver'][1]}")

    sdir = os.path.join(base_dir, "sessions")
    payloads = _payloads_with_intact_sidecars(sdir)

    for name in payloads:
        path = os.path.join(sdir, name)
        loaded = session.load_session(path)  # МАЄ читатися без винятку
        assert loaded is not None

    last_imported = results["importer"][0][-1]
    last_saved = results["saver"][0][-1]
    assert last_imported and os.path.exists(last_imported), (
        "останній import_session мав пережити паралельний save_session")
    assert last_saved and os.path.exists(last_saved), (
        "останній save_session мав пережити паралельний import_session")
    assert len(payloads) == KEEP, (
        f"очікувано рівно keep={KEEP} пар після серіалізованого prune, "
        f"отримано {len(payloads)}: {sorted(payloads)}")


@pytest.mark.parametrize("attempt", range(3))
def test_b3_import_session_does_not_interleave_with_concurrent_save(
        tmp_path_factory, attempt):
    """3 прогони поспіль (акцептанс мікроблоку H) — import_session у
    процесі 1 паралельно з save_session у процесі 2, спільний base_dir,
    10 раундів на прогін."""
    _run_b3_once(tmp_path_factory)
