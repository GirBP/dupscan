import os
import sys
import threading
import time

import blake3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def groups_as_sets(groups):
    return {tuple(sorted(os.path.basename(p) for p in g.paths)) for g in groups}


def test_pause_blocks_then_resumes(tmp_path):
    for top in ("A", "B"):
        for i in range(40):
            make(tmp_path / top / f"f{i}.bin", bytes([i % 251]) * 3000)

    pause = threading.Event()
    pause.set()
    cancel = threading.Event()
    ticks: list[tuple[str, int, int]] = []
    result: dict[str, core.ScanResult] = {}

    def progress(phase, done, total):
        ticks.append((phase, done, total))

    def run():
        result["r"] = core.scan(
            [str(tmp_path)], progress=progress, cancel=cancel, pause=pause
        )

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), "потік мав лишитись заблокованим на паузі"
    assert len(ticks) in (0, 1), "перший walk-чекпоінт стоїть до будь-якої роботи"

    pause.clear()
    t.join(30)
    assert not t.is_alive(), "потік мав завершитись після зняття паузи"

    r_paused = result["r"]
    assert r_paused.partial is False

    r_direct = core.scan([str(tmp_path)])
    assert groups_as_sets(r_paused.file_groups) == groups_as_sets(r_direct.file_groups)
    assert groups_as_sets(r_paused.dir_groups) == groups_as_sets(r_direct.dir_groups)


def test_cancel_while_paused(tmp_path):
    for i in range(20):
        make(tmp_path / f"f{i}.bin", bytes([i % 251]) * 4000)

    pause = threading.Event()
    pause.set()
    cancel = threading.Event()
    result: dict[str, core.ScanResult] = {}

    def run():
        result["r"] = core.scan([str(tmp_path)], cancel=cancel, pause=pause)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)
    assert t.is_alive(), "потік мав лишитись заблокованим на паузі"

    cancel.set()  # пауза НЕ знімається — cancel мусить розбудити wait самостійно
    t.join(5)
    assert not t.is_alive(), "cancel мав розбудити потік навіть під час паузи"
    assert result["r"].partial is True


def test_hash_file_pause_mid_chunks(tmp_path):
    data = os.urandom(4 * 1024 * 1024)  # > CHUNK (1 МіБ) -> кілька ітерацій чанк-циклу
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    size = p.stat().st_size

    pause = threading.Event()
    pause.set()
    cancel = threading.Event()
    result: dict[str, str | None] = {}

    def run():
        result["d"] = core._hash_file(
            str(p), size, full=True, cancel=cancel, pause=pause
        )

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.2)
    assert t.is_alive(), "потік мав заблокуватись у чанк-циклі"

    pause.clear()
    t.join(30)
    assert not t.is_alive()
    assert result["d"] == blake3.blake3(data).hexdigest()


def test_pause_none_backcompat(tmp_path):
    make(tmp_path / "a.bin", b"SAME" * 100)
    make(tmp_path / "b.bin", b"SAME" * 100)
    r = core.scan([str(tmp_path)])
    assert len(r.file_groups) == 1
