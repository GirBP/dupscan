import errno
import os
import sys
import threading

import blake3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_file_dup_groups_exact_only(tmp_path):
    make(tmp_path / "a/x.bin", b"SAME" * 1000)
    make(tmp_path / "b/y.bin", b"SAME" * 1000)
    make(tmp_path / "b/z.bin", b"SAME" * 999 + b"DIFF")  # той самий розмір, інший хвіст
    make(tmp_path / "u.bin", b"unique")
    r = core.scan([str(tmp_path)])
    assert len(r.file_groups) == 1
    assert sorted(os.path.basename(p) for p in r.file_groups[0].paths) == ["x.bin", "y.bin"]


def test_dir_dup_and_nested_suppression(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "doc.txt", b"D" * 500)
        make(tmp_path / top / "sub/img.bin", b"I" * 2000)
    r = core.scan([str(tmp_path)])
    names = [{os.path.basename(p) for p in g.paths} for g in r.dir_groups]
    assert {"A", "B"} in names
    assert {"sub"} not in [s for s in names]  # вкладені пригнічено


def test_similarity_partial_overlap(tmp_path):
    make(tmp_path / "C/shared.bin", b"S" * 4000)
    make(tmp_path / "C/own1.bin", b"1" * 3000)
    make(tmp_path / "D/shared.bin", b"S" * 4000)
    make(tmp_path / "D/own2.bin", b"2" * 5000)
    r = core.scan([str(tmp_path)])
    assert r.sim_pairs, "пара C~D мусить існувати"
    p = r.sim_pairs[0]
    assert {os.path.basename(p.dir_a), os.path.basename(p.dir_b)} == {"C", "D"}
    assert 0 < p.percent < 100
    assert p.shared_bytes == 4000


def test_similarity_promotes_nested_matches_to_large_parent_pair(tmp_path):
    for branch, shared_byte, own_a, own_b in (
            ("photos", b"P", b"a", b"b"),
            ("videos", b"V", b"c", b"d")):
        make(tmp_path / f"A/{branch}/shared.bin", shared_byte * 4000)
        make(tmp_path / f"B/{branch}/shared.bin", shared_byte * 4000)
        make(tmp_path / f"A/{branch}/only-a.bin", own_a * 1000)
        make(tmp_path / f"B/{branch}/only-b.bin", own_b * 1200)

    result = core.scan([str(tmp_path)])
    keys = [(p.dir_a, p.dir_b) for p in result.sim_pairs]
    parent = tuple(sorted((str(tmp_path / "A"), str(tmp_path / "B"))))
    photos = tuple(sorted((
        str(tmp_path / "A/photos"), str(tmp_path / "B/photos"))))
    videos = tuple(sorted((
        str(tmp_path / "A/videos"), str(tmp_path / "B/videos"))))

    assert parent in keys
    assert photos in keys
    assert videos in keys
    parent_pair = result.sim_pairs[keys.index(parent)]
    assert parent_pair.shared_bytes == 8000
    assert keys[0] == parent, "сукупно більша parent pair має бути першою"


def test_high_fanout_duplicate_class_still_seeds_bounded_similarity():
    result = core.ScanResult()
    shared_class = f"100:{'a' * 64}"
    shared_paths = []
    for index in range(core.FANOUT_CAP + 12):
        directory = f"/bench/archive-{index:03d}"
        shared = f"{directory}/shared-{index:03d}.bin"
        unique = f"{directory}/unique-{index:03d}.bin"
        result.dir_ok[directory] = True
        result.dir_files[directory] = [shared, unique]
        result.file_meta[shared] = core.FileInfo(shared, 100, 1, 1)
        result.file_meta[unique] = core.FileInfo(unique, index + 1, 1, 1)
        result.file_class[shared] = shared_class
        result.file_class[unique] = f"u:{index + 1}"
        shared_paths.append(shared)
    result.class_size[shared_class] = 100
    result.class_paths[shared_class] = sorted(shared_paths)

    core._aggregate(result)

    assert result.sim_pairs
    assert len(result.sim_pairs) <= core.MAX_PAIRS
    assert all(pair.shared_bytes == 100 for pair in result.sim_pairs)


def test_identical_dirs_not_in_similarity(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "f.bin", b"F" * 1000)
    r = core.scan([str(tmp_path)])
    assert len(r.dir_groups) == 1
    assert r.sim_pairs == []  # 100%-дублікати живуть у вкладці «Папки»


def test_symlinks_bundles_hardlinks(tmp_path):
    make(tmp_path / "A/real.bin", b"R" * 1000)
    os.symlink(tmp_path / "A/real.bin", tmp_path / "A/link.bin")
    app = tmp_path / "Thing.app/Contents"
    make(app / "res.bin", b"R" * 1000)  # bundle сканується як звичайна тека
    make(tmp_path / "hl.bin", b"H" * 1000)
    os.link(tmp_path / "hl.bin", tmp_path / "hl2.bin")  # 1 фізичне тіло
    r = core.scan([str(tmp_path)])
    assert len(r.file_groups) == 1
    assert {os.path.basename(p) for p in r.file_groups[0].paths} == {
        "real.bin", "res.bin"
    }
    assert str(tmp_path / "A/link.bin") not in r.file_meta
    assert sum(p.endswith(("hl.bin", "hl2.bin")) for p in r.file_meta) == 1


def test_cancel_stops(tmp_path):
    for i in range(50):
        make(tmp_path / f"f{i}.bin", bytes([i % 251]) * 10000)
    ev = threading.Event()
    ev.set()
    r = core.scan([str(tmp_path)], cancel=ev)
    assert r.file_groups == [] and r.dir_groups == []


def test_unreadable_dir_never_grouped(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 800)
    make(tmp_path / "A/secret/inner.bin", b"S" * 500)
    os.chmod(tmp_path / "A/secret", 0o000)
    try:
        r = core.scan([str(tmp_path)])
        names = [{os.path.basename(p) for p in g.paths} for g in r.dir_groups]
        assert {"A", "B"} not in names  # A недоказова -> ніколи не дублікат
    finally:
        os.chmod(tmp_path / "A/secret", 0o755)


def test_simpair_lists_shared_files(tmp_path):
    make(tmp_path / "C/shared.bin", b"S" * 4000)
    make(tmp_path / "C/own1.bin", b"1" * 3000)
    make(tmp_path / "D/shared.bin", b"S" * 4000)
    make(tmp_path / "D/own2.bin", b"2" * 5000)
    r = core.scan([str(tmp_path)])
    p = r.sim_pairs[0]
    assert p.shared, "пара мусить перелічувати спільні файли"
    size, fa, fb = p.shared[0]
    assert size == 4000
    assert os.path.basename(fa) == "shared.bin" and os.path.basename(fb) == "shared.bin"
    assert fa.startswith(p.dir_a) and fb.startswith(p.dir_b)


def test_recompute_after_file_removal(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 1000)
        make(tmp_path / top / "y.bin", b"Y" * 2000)
    r = core.scan([str(tmp_path)])
    assert len(r.dir_groups) == 1 and len(r.file_groups) == 2
    victim = str(tmp_path / "A/x.bin")
    os.remove(victim)
    core.recompute(r, {victim})
    # файлова група x лишилась із одним шляхом -> зникла; y-група жива
    assert len(r.file_groups) == 1
    # A більше не ідентична B -> групи папок нема, натомість подібність
    assert r.dir_groups == []
    assert r.sim_pairs and r.sim_pairs[0].shared_bytes == 2000


def test_recompute_after_dir_removal(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 1000)
    r = core.scan([str(tmp_path)])
    assert len(r.dir_groups) == 1
    import shutil
    shutil.rmtree(tmp_path / "A")
    core.recompute(r, {str(tmp_path / "A")})
    assert r.dir_groups == [] and r.file_groups == [] and r.sim_pairs == []


def test_btime_collected(tmp_path):
    make(tmp_path / "a.bin", b"A" * 100)
    r = core.scan([str(tmp_path)])
    fi = r.file_meta[str(tmp_path / "a.bin")]
    assert fi.btime_ns > 0 and fi.mtime_ns > 0


def test_big_file_hash_identical_single_vs_multithread(tmp_path, monkeypatch):
    chunk = os.urandom(2 * 1024 * 1024)
    data = chunk * 9  # ~18 МіБ, вище дефолтного порогу core.BIG
    p = tmp_path / "big.bin"
    p.write_bytes(data)
    size = p.stat().st_size
    assert size > core.BIG, "тестові дані мають перевищувати поріг багатопотокового шляху"

    multi_digest = core._hash_file(str(p), size, full=True, cancel=threading.Event())

    monkeypatch.setattr(core, "BIG", size + 1)  # форсувати однопотоковий шлях
    single_digest = core._hash_file(str(p), size, full=True, cancel=threading.Event())

    want = blake3.blake3(data).hexdigest()
    assert multi_digest == want
    assert single_digest == want
    assert multi_digest == single_digest


def test_adaptive_blake3_budget_and_large_file_progress(
        tmp_path, monkeypatch):
    monkeypatch.setattr(core.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(core, "BIG", 1024)
    monkeypatch.setattr(core, "BIG_CHUNK", 256 * 1024)
    assert core._blake3_threads(2048, True, 1) == 8
    assert core._blake3_threads(2048, True, 2) == 4
    assert core._blake3_threads(2048, True, 8) == 1
    assert core._blake3_threads(512, True, 1) == 1
    assert core._blake3_threads(2048, False, 1) == 1

    data = b"Z" * (2 * 1024 * 1024)
    make(tmp_path / "A/large.bin", data)
    make(tmp_path / "B/large.bin", data)
    observed_threads = []
    real_hash = core._hash_file

    def traced(path, size, full, cancel, *args, **kwargs):
        if full:
            observed_threads.append(kwargs.get("max_threads"))
        return real_hash(path, size, full, cancel, *args, **kwargs)

    clock = [0.0]

    def advancing_clock():
        clock[0] += 0.11
        return clock[0]

    monkeypatch.setattr(core, "_hash_file", traced)
    monkeypatch.setattr(core.time, "monotonic", advancing_clock)
    progress = []
    result = core.scan(
        [str(tmp_path)],
        progress=lambda phase, done, total: progress.append(
            (phase, done, total)),
    )

    assert len(result.file_groups) == 1
    assert observed_threads == [4, 4]
    full_ticks = [
        (done, total)
        for phase, done, total in progress
        if phase.startswith("Повне хешування")
    ]
    assert any(0 < done < total for done, total in full_ticks)
    assert full_ticks[-1][0] == full_ticks[-1][1]


def test_aggregation_pause_blocks_without_publishing_partial_state(tmp_path):
    for folder in ("A", "B"):
        make(tmp_path / folder / "same.bin", b"same")
    result = core.scan([str(tmp_path)])
    result.file_groups = []
    result.dir_groups = []
    result.sim_pairs = []
    pause = threading.Event()
    pause.set()
    cancel = threading.Event()
    finished = threading.Event()

    def aggregate():
        core._aggregate(result, cancel=cancel, pause=pause)
        finished.set()

    worker = threading.Thread(target=aggregate)
    worker.start()
    assert not finished.wait(0.1)
    assert result.file_groups == []
    pause.clear()
    worker.join(2)
    assert finished.is_set()
    assert result.file_groups


def test_cancel_during_scan_aggregation_returns_exact_only_partial(
    tmp_path,
    monkeypatch,
):
    for folder in ("A", "B"):
        make(tmp_path / folder / "same.bin", b"same")
    cancel = threading.Event()
    pause = threading.Event()
    received = {}

    def cancel_aggregate(_result, **kwargs):
        received.update(kwargs)
        cancel.set()
        raise OSError(errno.ECANCELED, "cancelled during aggregation")

    monkeypatch.setattr(core, "_aggregate", cancel_aggregate)
    result = core.scan(
        [str(tmp_path)],
        cancel=cancel,
        pause=pause,
        progress=lambda *_args: None,
    )

    assert received["cancel"] is cancel
    assert received["pause"] is pause
    assert callable(received["progress"])
    assert result.partial
    assert len(result.file_groups) == 1
    assert result.dir_groups == []
    assert result.sim_pairs == []
