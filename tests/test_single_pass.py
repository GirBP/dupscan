"""A1: файл ≤ PARTIAL читається рівно один раз; проба = фінальний клас.
Критична безпека: однакова голова + різний хвіст НЕ групується."""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.cache as cache_mod  # noqa: E402
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def spy_reads(monkeypatch):
    reads: dict[str, list[bool]] = {}
    real = core._hash_file

    def spy(path, size, full, cancel, pause=None, *a, **k):
        reads.setdefault(path, []).append(full)
        return real(path, size, full, cancel, pause, *a, **k)

    monkeypatch.setattr(core, "_hash_file", spy)
    return reads


def test_small_files_read_exactly_once(tmp_path, monkeypatch):
    reads = spy_reads(monkeypatch)
    small = b"S" * 4096  # << PARTIAL
    for name in ("a", "b", "c"):
        make(tmp_path / name / "dup.bin", small)
    make(tmp_path / "a/uniq.bin", b"U" * 5000)
    r = core.scan([str(tmp_path)])
    assert len(r.file_groups) == 1 and len(r.file_groups[0].paths) == 3
    for p, calls in reads.items():
        if os.path.getsize(p) <= core.PARTIAL:
            assert calls == [False], (
                f"{p}: малий файл мусить читатись РІВНО раз (probe), було {calls}"
            )


def test_boundary_partial_exact_and_plus_one(tmp_path, monkeypatch):
    reads = spy_reads(monkeypatch)
    exact = b"E" * core.PARTIAL
    plus = b"P" * (core.PARTIAL + 1)
    make(tmp_path / "x1/f.bin", exact)
    make(tmp_path / "x2/f.bin", exact)
    make(tmp_path / "y1/g.bin", plus)
    make(tmp_path / "y2/g.bin", plus)
    r = core.scan([str(tmp_path)])
    sizes = sorted(g.size for g in r.file_groups)
    assert sizes == [core.PARTIAL, core.PARTIAL + 1]
    for p, calls in reads.items():
        if "f.bin" in p:
            assert calls == [False], "== PARTIAL: один прохід"
        if "g.bin" in p:
            assert calls == [False, True], "PARTIAL+1: проба + повний"


def test_same_head_different_tail_not_grouped(tmp_path):
    head = b"H" * core.PARTIAL
    make(tmp_path / "a/t.bin", head + b"TAIL-ONE")
    make(tmp_path / "b/t.bin", head + b"TAIL-TWO")
    r = core.scan([str(tmp_path)])
    assert r.file_groups == [], "різні хвости за спільною головою — НЕ дублікати"


def test_settled_small_files_cached_as_full(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    small = b"C" * 8192
    make(tmp_path / "t/a/dup.bin", small)
    make(tmp_path / "t/b/dup.bin", small)
    c = cache_mod.HashCache.open(str(tmp_path / "data"))
    try:
        r = core.scan([str(tmp_path / "t")], cache=c)
        assert len(r.file_groups) == 1
        c.flush()
        p = str(tmp_path / "t/a/dup.bin")
        st = os.lstat(p)
        got = c.get(p, st.st_size, st.st_mtime_ns, "f")
        assert got, "осілий малий файл мусить мати kind='f' рядок у кеші"
        assert got == r.file_groups[0].digest.split(":", 1)[1]
    finally:
        c.close()
