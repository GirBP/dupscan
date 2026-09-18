import errno
import gzip
import json
import os
import sys
import threading
import time

import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def group_paths(groups) -> set[tuple[str, ...]]:
    return {tuple(sorted(g.paths)) for g in groups}


def sim_sig(pairs) -> set[tuple[tuple[str, str], int]]:
    return {(tuple(sorted((p.dir_a, p.dir_b))), p.shared_bytes) for p in pairs}


def test_round_trip_preserves_groups_and_partial_flag(tmp_path):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
        make(tree / top / "y.bin", b"Y" * (core.PARTIAL + 2000))
    r1 = core.scan([str(tree)])
    assert r1.partial is False

    path = session.save_session(
        r1, [str(tree)], partial=r1.partial, base_dir=str(data_dir)
    )
    assert path and os.path.exists(path)

    r2 = session.load_session(path)
    assert group_paths(r2.file_groups) == group_paths(r1.file_groups)
    assert group_paths(r2.dir_groups) == group_paths(r1.dir_groups)
    assert sim_sig(r2.sim_pairs) == sim_sig(r1.sim_pairs)
    assert r2.partial is False
    assert r2.files_seen == r1.files_seen
    assert r2.bytes_seen == r1.bytes_seen


def test_load_then_recompute_removes_missing_path(tmp_path):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
    r1 = core.scan([str(tree)])
    assert len(r1.dir_groups) == 1
    path = session.save_session(r1, [str(tree)], base_dir=str(data_dir))

    victim = str(tree / "A" / "x.bin")
    os.remove(victim)

    r2 = session.load_session(path)
    assert victim in r2.file_meta  # знімок сесії ще пам'ятає файл
    core.recompute(r2, {victim})
    assert r2.dir_groups == []
    assert r2.file_groups == []
    assert victim not in r2.file_meta


def test_cancelled_scan_sets_partial_and_still_aggregates(tmp_path):
    tree = tmp_path / "tree"
    for i in range(20):
        make(tree / f"f{i}.bin", bytes([i % 251]) * 5000)
    ev = threading.Event()
    ev.set()  # скасовано ще до старту -> рання зупинка у walk
    r = core.scan([str(tree)], cancel=ev)
    assert r.partial is True
    # рання зупинка у walk -> взагалі нічого не встигли занести
    assert r.file_groups == [] and r.dir_groups == []


def test_partial_flag_persists_through_save_and_load(tmp_path):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    for i in range(20):
        make(tree / f"f{i}.bin", bytes([i % 251]) * 5000)
    ev = threading.Event()
    ev.set()
    r1 = core.scan([str(tree)], cancel=ev)
    assert r1.partial is True

    path = session.save_session(
        r1, [str(tree)], partial=r1.partial, base_dir=str(data_dir)
    )
    r2 = session.load_session(path)
    assert r2.partial is True


def test_cancel_mid_hash_sets_partial_and_never_fabricates_a_group(
    tmp_path, monkeypatch
):
    # Скасування всередині пулу — стан гонки за дизайном (спільний cancel на
    # усі паралельні воркери), тож тут перевіряємо детермінований інваріант
    # безпеки (перерваний файл НІКОЛИ не потрапляє у групу), а не яка саме
    # інша пара "встигла" — це залежить від планування потоків.
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "y.bin", b"Y" * (core.PARTIAL + 2000))

    victim = str(tree / "A" / "y.bin")
    orig = core._hash_file

    def flaky(path, size, full, cancel, pause=None, **kwargs):
        if full and path == victim:
            cancel.set()
            return None
        return orig(path, size, full, cancel, pause, **kwargs)

    monkeypatch.setattr(core, "_hash_file", flaky)
    r = core.scan([str(tree)])

    assert r.partial is True
    assert not any(victim in g.paths for g in r.file_groups)
    assert r.file_class.get(victim, "").startswith("u:")


def test_list_sessions_sorted_newest_first_and_delete(tmp_path):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    make(tree / "a.bin", b"A" * 100)
    r = core.scan([str(tree)])

    paths = []
    for _ in range(3):
        p = session.save_session(r, [str(tree)], base_dir=str(data_dir))
        paths.append(p)
        time.sleep(0.002)

    metas = session.list_sessions(base_dir=str(data_dir))
    assert [m["path"] for m in metas] == list(reversed(paths))
    assert metas[0]["created_ns"] >= metas[1]["created_ns"] >= metas[2]["created_ns"]
    for m in metas:
        assert m["roots"] == [str(tree)]
        assert m["partial"] is False
        assert "counts" in m and set(m["counts"]) == {"files", "dirs", "pairs"}

    session.delete_session(paths[-1])
    metas2 = session.list_sessions(base_dir=str(data_dir))
    assert len(metas2) == 2
    assert paths[-1] not in [m["path"] for m in metas2]


def test_list_sessions_skips_corrupted_json(tmp_path):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    make(tree / "a.bin", b"A" * 100)
    r = core.scan([str(tree)])
    session.save_session(r, [str(tree)], base_dir=str(data_dir))

    sdir = os.path.join(str(data_dir), "sessions")
    with open(os.path.join(sdir, "not-json-at-all.json"), "w") as fh:
        fh.write("{ broken")

    metas = session.list_sessions(base_dir=str(data_dir))
    assert len(metas) == 1


def test_save_session_best_effort_on_unwritable_dir(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o000)
    tree = tmp_path / "tree"
    make(tree / "a.bin", b"A" * 100)
    r = core.scan([str(tree)])
    try:
        path = session.save_session(r, [str(tree)], base_dir=str(blocked / "nested"))
        assert path == "" or not os.path.exists(path)
    finally:
        os.chmod(blocked, 0o755)


def test_save_session_prunes_history_to_30(tmp_path):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    make(tree / "a.bin", b"A" * 100)
    r = core.scan([str(tree)])
    for _ in range(33):
        session.save_session(r, [str(tree)], base_dir=str(data_dir))
        time.sleep(0.0005)
    metas = session.list_sessions(base_dir=str(data_dir))
    assert len(metas) <= 30


def test_session_roundtrip_preserves_total_problem_count(tmp_path):
    result = core.ScanResult()
    result.errors = ["detail-a", "detail-b"]
    result.errors_total = 27
    path = session.save_session(
        result, [str(tmp_path)], base_dir=str(tmp_path / "data"))

    loaded = session.load_session(path)

    assert loaded.errors == result.errors
    assert loaded.errors_total == 27


def test_streaming_session_load_can_be_cancelled_before_publication(tmp_path):
    tree = tmp_path / "tree"
    make(tree / "a.bin", b"A" * 100)
    result = core.scan([str(tree)])
    path = session.save_session(
        result, [str(tree)], base_dir=str(tmp_path / "data"))
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(OSError) as raised:
        session.load_session(path, cancel=cancel)

    assert raised.value.errno == errno.ECANCELED


def test_streaming_session_load_reports_read_validate_and_group_progress(tmp_path):
    tree = tmp_path / "tree"
    for index in range(200):
        make(tree / f"item-{index:03d}.bin", bytes([index % 251]) * 20)
    result = core.scan([str(tree)])
    path = session.save_session(
        result, [str(tree)], base_dir=str(tmp_path / "data"))
    phases = []

    loaded = session.load_session(
        path,
        progress=lambda phase, _done, _total: phases.append(phase),
    )

    assert loaded.files_seen == result.files_seen
    assert "Читаю сесію" in phases
    assert "Перевіряю структуру сесії" in phases
    assert "Групую результати сесії" in phases


def test_historical_session_load_never_resolves_offline_roots(
        tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    make(tree / "a.bin", b"A" * 100)
    result = core.scan([str(tree)])
    path = session.save_session(
        result, [str(tree)], base_dir=str(tmp_path / "data"))
    monkeypatch.setattr(
        session.os.path,
        "realpath",
        lambda _path: pytest.fail(
            "read-only session parsing must not touch historical roots"),
    )

    loaded = session.load_session(path)

    assert loaded.files_seen == 1


def test_streaming_loader_rejects_unhashable_ignored_pair_as_value_error(
        tmp_path):
    root = str(tmp_path / "root")
    payload = {
        "version": 2,
        "created_ns": 1,
        "roots": [root],
        "state": {
            "file_meta": {},
            "file_class": {},
            "class_size": {},
            "class_paths": {},
            "dir_files": {},
            "dir_children": {},
            "dir_links": {},
            "dir_aliases": {},
            "dir_ok": {},
            "ignored_pairs": [[{"bad": "shape"}, root]],
        },
    }
    path = tmp_path / "malformed.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    with pytest.raises(ValueError, match="ігнорована пара"):
        session.load_session(str(path))
