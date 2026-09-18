import os
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.cache as cache  # noqa: E402
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_cache_get_put_roundtrip_and_invalidation(tmp_path):
    hc = cache.HashCache.open(base_dir=str(tmp_path / "data"))
    hc.put_many([("a.bin", "f", 100, 111, "deadbeef")])
    assert hc.get("a.bin", 100, 111, "f") == "deadbeef"
    assert hc.get("a.bin", 999, 111, "f") is None  # розмір змінився
    assert hc.get("a.bin", 100, 222, "f") is None  # mtime змінився
    assert hc.get("a.bin", 100, 111, "p") is None  # інший kind — інший ключ
    assert hc.get("missing.bin", 100, 111, "f") is None
    hc.close()


def test_get_many_batches_and_respects_size_mtime(tmp_path):
    hc = cache.HashCache.open(base_dir=str(tmp_path / "data"))
    hc.put_many(
        [
            ("a.bin", "f", 100, 111, "digest-a"),
            ("b.bin", "f", 200, 222, "digest-b"),
            ("c.bin", "f", 300, 333, "digest-c"),
            ("a.bin", "p", 100, 111, "partial-a"),  # інший kind — не має протекти
        ]
    )
    hits = hc.get_many(
        "f",
        [
            ("a.bin", 100, 111),  # хіт
            ("b.bin", 999, 222),  # розмір змінився -> miss
            ("c.bin", 300, 333),  # хіт
            ("missing.bin", 1, 1),  # немає рядка
        ],
    )
    assert hits == {"a.bin": "digest-a", "c.bin": "digest-c"}
    hc.close()


def test_get_many_chunks_over_500_items(tmp_path):
    hc = cache.HashCache.open(base_dir=str(tmp_path / "data"))
    rows = [(f"f{i}.bin", "f", i, i, f"digest-{i}") for i in range(1200)]
    hc.put_many(rows)
    items = [(f"f{i}.bin", i, i) for i in range(1200)]
    hits = hc.get_many("f", items)
    assert len(hits) == 1200
    assert hits["f0.bin"] == "digest-0"
    assert hits["f999.bin"] == "digest-999"
    assert hits["f1199.bin"] == "digest-1199"
    hc.close()


def test_bulk_cache_read_and_write_honor_cancel_between_batches(tmp_path):
    hc = cache.HashCache.open(base_dir=str(tmp_path / "data"))
    rows = [(f"f{i}.bin", "f", i, i, f"digest-{i}") for i in range(1200)]
    cancel = threading.Event()
    cancel.set()

    hc.put_many(rows, cancel=cancel)
    assert hc.get_many(
        "f", [(f"f{i}.bin", i, i) for i in range(1200)],
        cancel=cancel,
    ) == {}

    cancel.clear()
    assert hc.get_many("f", [("f0.bin", 0, 0)]) == {}
    hc.close()


def test_get_many_empty_or_disabled_returns_empty_dict(tmp_path):
    hc = cache.HashCache.open(base_dir=str(tmp_path / "data"))
    assert hc.get_many("f", []) == {}
    hc.close()
    data_dir = tmp_path / "blocked"
    data_dir.mkdir()
    (data_dir / "hashes.db").mkdir()
    hc2 = cache.HashCache.open(base_dir=str(data_dir))
    assert hc2.get_many("f", [("a.bin", 1, 1)]) == {}


def test_warm_rescan_skips_hash_file_and_matches_cold_result(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    make(tree / "a/x.bin", b"SAME" * 1000)
    make(tree / "b/y.bin", b"SAME" * 1000)
    make(tree / "b/z.bin", b"SAME" * 999 + b"DIFF")
    make(tree / "u.bin", b"unique")

    r_nocache = core.scan([str(tree)])

    hc1 = cache.HashCache.open(base_dir=str(data_dir))
    r_cold = core.scan([str(tree)], cache=hc1)
    hc1.close()

    def groups_as_sets(groups):
        return {tuple(sorted(os.path.basename(p) for p in g.paths)) for g in groups}

    assert groups_as_sets(r_cold.file_groups) == groups_as_sets(r_nocache.file_groups)

    calls = [0]
    orig = core._hash_file

    def counting(*a, **kw):
        calls[0] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(core, "_hash_file", counting)
    hc2 = cache.HashCache.open(base_dir=str(data_dir))
    r_warm = core.scan([str(tree)], cache=hc2)
    hc2.close()

    assert calls[0] == 0, (
        "теплий скан не мав викликати _hash_file для незмінених файлів"
    )
    assert groups_as_sets(r_warm.file_groups) == groups_as_sets(r_nocache.file_groups)


def test_modified_file_is_rehashed_unchanged_sibling_is_not(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    make(tree / "a/x.bin", b"SAME" * 1000)
    make(tree / "b/y.bin", b"SAME" * 1000)

    hc1 = cache.HashCache.open(base_dir=str(data_dir))
    core.scan([str(tree)], cache=hc1)
    hc1.close()

    import time

    time.sleep(0.01)
    # той самий розмір (4000 Б), інший вміст і mtime -> кеш-miss, не uniq за розміром
    make(tree / "a/x.bin", b"SAME" * 999 + b"DIFF")

    seen_paths = []
    orig = core._hash_file

    def tracking(path, size, full, cancel, pause=None, **kwargs):
        seen_paths.append(path)
        return orig(path, size, full, cancel, pause, **kwargs)

    monkeypatch.setattr(core, "_hash_file", tracking)
    hc2 = cache.HashCache.open(base_dir=str(data_dir))
    core.scan([str(tree)], cache=hc2)
    hc2.close()

    changed = str(tree / "a/x.bin")
    unchanged = str(tree / "b/y.bin")
    assert changed in seen_paths
    assert unchanged not in seen_paths


def test_best_effort_survives_corrupted_db_path(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "hashes.db").mkdir()  # тека замість файлу -> sqlite3.connect впаде

    hc = cache.HashCache.open(base_dir=str(data_dir))
    assert hc.get("whatever.bin", 1, 1, "f") is None
    hc.put_many([("whatever.bin", "f", 1, 1, "digest")])  # не має кинути

    tree = tmp_path / "tree"
    make(tree / "a/x.bin", b"SAME" * 1000)
    make(tree / "b/y.bin", b"SAME" * 1000)
    r = core.scan([str(tree)], cache=hc)
    assert len(r.file_groups) == 1
    hc.close()


def test_cancelled_full_read_is_never_cached(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    tree = tmp_path / "tree"
    big = core.PARTIAL + 5000  # > PARTIAL: full-стадія існує (малі осідають з проби)
    make(tree / "a.bin", b"X" * big)
    make(tree / "b.bin", b"X" * big)  # однаковий розмір і вміст -> кандидати full-хешу

    victim = str(tree / "a.bin")
    orig = core._hash_file

    def flaky(path, size, full, cancel, pause=None, **kwargs):
        if full and path == victim:
            cancel.set()
            return None
        return orig(path, size, full, cancel, pause, **kwargs)

    monkeypatch.setattr(core, "_hash_file", flaky)
    hc = cache.HashCache.open(base_dir=str(data_dir))
    core.scan([str(tree)], cache=hc)
    hc.close()

    hc2 = cache.HashCache.open(base_dir=str(data_dir))
    st = os.stat(victim)
    assert hc2.get(victim, st.st_size, st.st_mtime_ns, "f") is None
    hc2.close()


def test_open_never_raises_on_unwritable_base_dir(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o000)
    try:
        hc = cache.HashCache.open(base_dir=str(blocked / "nested" / "data"))
        assert hc.get("x", 1, 1, "f") is None
        hc.put_many([("x", "f", 1, 1, "d")])  # no-op, не має кинути
        hc.close()
    finally:
        os.chmod(blocked, 0o755)


def test_context_manager_closes(tmp_path):
    with cache.HashCache.open(base_dir=str(tmp_path / "data")) as hc:
        assert hc.get("x", 1, 1, "f") is None


def test_open_removes_only_unusable_legacy_identity_rows(tmp_path):
    data = tmp_path / "data"
    hc = cache.HashCache.open(base_dir=str(data))
    hc.put_many([
        ("legacy", "f", 1, 1, "old"),
        ("strict", "f", 2, 2, 3, 4, 5, "new"),
    ])
    hc.close()

    cache.HashCache.open(base_dir=str(data)).close()
    with sqlite3.connect(data / "hashes.db") as conn:
        paths = {row[0] for row in conn.execute("SELECT path FROM hashes")}
    assert paths == {"strict"}


def test_cache_hit_refreshes_access_time_without_rehash(tmp_path):
    data = tmp_path / "data"
    hc = cache.HashCache.open(base_dir=str(data))
    hc.put_many([("strict", "f", 2, 2, 3, 4, 5, "digest")])
    hc.close()
    old = time.time_ns() - 8 * 24 * 60 * 60 * 1_000_000_000
    with sqlite3.connect(data / "hashes.db") as conn:
        conn.execute("UPDATE hashes SET accessed_ns=?", (old,))
        conn.commit()

    hc = cache.HashCache.open(base_dir=str(data))
    assert hc.get_many("f", [("strict", 2, 2, 3, 4, 5)]) == {
        "strict": "digest"
    }
    hc.close()
    with sqlite3.connect(data / "hashes.db") as conn:
        touched = conn.execute(
            "SELECT accessed_ns FROM hashes WHERE path='strict'"
        ).fetchone()[0]
    assert touched > old


def test_cache_row_cap_keeps_newest_entries(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(cache, "_MAX_ROWS", 2)
    hc = cache.HashCache.open(base_dir=str(data))
    for index in range(3):
        hc.put_many([
            (f"p{index}", "f", index + 1, index + 1,
             index + 1, index + 1, index + 1, f"d{index}")
        ])
        time.sleep(0.001)
    hc.close()

    cache.HashCache.open(base_dir=str(data)).close()
    with sqlite3.connect(data / "hashes.db") as conn:
        paths = {row[0] for row in conn.execute("SELECT path FROM hashes")}
    assert paths == {"p1", "p2"}


def test_cache_maintenance_reports_progress_and_honors_cancel(tmp_path):
    data = tmp_path / "data"
    hc = cache.HashCache.open(base_dir=str(data))
    hc.put_many([
        (f"legacy-{index}", "f", index + 1, index + 1, f"d{index}")
        for index in range(10)
    ])
    hc.close()
    cancel = threading.Event()
    cancel.set()

    cache.HashCache.open(
        base_dir=str(data), cancel=cancel,
        progress=lambda *_args: None).close()
    with sqlite3.connect(data / "hashes.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 10

    ticks = []
    cache.HashCache.open(
        base_dir=str(data), progress=lambda *args: ticks.append(args)).close()
    with sqlite3.connect(data / "hashes.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 0
    assert ticks and ticks[-1][0] == "Очищення кешу"
