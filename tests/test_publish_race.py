"""Конкурентний тест fsops._publish_no_overwrite.

«Гонитва двох процесів у publish-фолбеку — rename перезаписує ЛИШЕ
власну заглушку» мала лише логічний доказ, машинного не було. На ФС без жорстких посилань (exFAT —
диск власника) `_publish_no_overwrite` резервує ім'я через O_EXCL і
перейменовує джерело ПОВЕРХ ЦІЄЇ САМОЇ заглушки — фолбек живе лише там,
на APFS/HFS+ спрацьовує швидший os.link і рейс-вікна цього виду немає.

Два процеси (multiprocessing, spawn — не fork: справжній окремий
інтерпретатор, а не скопійований стан) публікують РІЗНИЙ вміст під ОДНЕ
ім'я в одній теці змонтованого exFAT-образу, стартуючи одночасно через
Barrier. Інваріант: після обох — рівно два файли (ціль і суфікс « (2)»),
кожен побайтово дорівнює РІВНО одному з двох джерел, без 0-байтових
сиріт і без суміші вмісту. ≥20 раундів у циклі — ловити вікно гонитви.

Фіксів тут не очікується (логічний доказ уже є в
_publish_no_overwrite: rename перезаписує лише щойно створену власну
O_EXCL-заглушку, тому чужі дані ніколи не гинуть) — це закриває
цю загрозу доказом, а не змінює код. Якщо тест НЕСТАБІЛЬНИЙ — падіння
документується чесно, без підгону тесту під зелене.
"""

from __future__ import annotations

import multiprocessing
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dupscan.domain.core as core  # noqa: E402
from qa.fs_images import hdiutil_available, mount_image, unmount  # noqa: E402
from qa.publish_race_worker import run_publish_round  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("DUPSCAN_FS_MATRIX") != "1" or not hdiutil_available(),
    reason="потрібен hdiutil: увімкнути DUPSCAN_FS_MATRIX=1",
)

ROUNDS = 20
JOIN_TIMEOUT = 30


def _run_one_round(root: str, round_id: int) -> None:
    dirpath = os.path.join(root, f"r{round_id}")
    os.makedirs(dirpath, exist_ok=True)
    dest_name = "target.bin"
    # Дешеві дрібні файли (раундів багато) — але побайтово РІЗНІ, щоб
    # суміш вмісту чи підміну джерела можна було відрізнити від успіху.
    payload_a = (f"payload-A-round-{round_id}-".encode()) * 37
    payload_b = (f"payload-B-round-{round_id}-".encode()) * 41
    assert payload_a != payload_b and len(payload_a) != len(payload_b)

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    procs = [
        ctx.Process(
            target=run_publish_round,
            args=(dirpath, "A", payload_a, dest_name, barrier, queue)),
        ctx.Process(
            target=run_publish_round,
            args=(dirpath, "B", payload_b, dest_name, barrier, queue)),
    ]
    for p in procs:
        p.start()

    results: dict[str, tuple[str | None, str | None]] = {}
    for _ in range(2):
        tag, published, error = queue.get(timeout=JOIN_TIMEOUT)
        results[tag] = (published, error)

    for p in procs:
        p.join(timeout=JOIN_TIMEOUT)
        assert p.exitcode == 0, (
            f"раунд {round_id}: воркер {p.name} завершився з exitcode="
            f"{p.exitcode} (мав звітувати через queue, а не впасти мовчки)")

    assert results["A"][1] is None and results["B"][1] is None, (
        f"раунд {round_id}: воркер повідомив помилку: {results}")

    published_names = {results["A"][0], results["B"][0]}
    assert None not in published_names, (
        f"раунд {round_id}: хтось не зміг опублікуватися жодним іменем: {results}")
    assert published_names == {"target.bin", "target (2).bin"}, (
        f"раунд {round_id}: очікувались рівно ціль і суфікс « (2)», "
        f"отримано: {published_names}")
    assert results["A"][0] != results["B"][0], (
        f"раунд {round_id}: обидва опублікувались під тим самим іменем "
        f"(перезапис чужих даних): {results}")

    # AppleDouble-супутники (macOS/exFAT кладе «._X» на КОЖЕН створений
    # файл, включно з полем-жертвою при некерованій гонитві — перевірено
    # окремо на одиночному незмагальному записі) — шум, не доказ гонитви;
    # is_appledouble фільтрує так само, як гейт Кошика й скан.
    entries = sorted(
        name for name in os.listdir(dirpath) if not core.is_appledouble(name))
    assert entries == ["target (2).bin", "target.bin"], (
        f"раунд {round_id}: неочікувані записи в теці (сирота/нестача): {entries}")

    on_disk: dict[str, bytes] = {}
    for name in entries:
        path = os.path.join(dirpath, name)
        size = os.stat(path).st_size
        assert size > 0, f"раунд {round_id}: 0-байтовий сирота {name}"
        with open(path, "rb") as fh:
            on_disk[name] = fh.read()

    # Кожен опублікований файл відповідає РІВНО тому джерелу, яке його туди
    # поклало — без суміші (наприклад, half-A-half-B через недописаний rename).
    expected = {"A": payload_a, "B": payload_b}
    for tag in ("A", "B"):
        name = results[tag][0]
        assert on_disk[name] == expected[tag], (
            f"раунд {round_id}: вміст {name} не відповідає джерелу {tag} "
            f"(підміна або суміш вмісту)")


def test_publish_no_overwrite_survives_two_process_race():
    _image, mount_point = mount_image("ExFAT", size_mb=64)
    try:
        root = os.path.join(mount_point, "race")
        os.makedirs(root, exist_ok=True)
        for round_id in range(ROUNDS):
            _run_one_round(root, round_id)
    finally:
        unmount(mount_point)
