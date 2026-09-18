"""S3 «Solid Core»: сесія v3 без похідних розділів.

class_paths відновлюється з file_class+class_size, dir_files — з
dir_ok+file_meta. Старі сесії v1/v2 читаються як раніше.
"""

import gzip
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _scan_tree(tmp_path):
    """Дерево з дублікатами, унікальними файлами і ПОРОЖНЬОЮ текою."""
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
        make(tree / top / "y.bin", b"Y" * 2000)
    make(tree / "solo.bin", b"S" * 300)
    (tree / "empty").mkdir()
    return tree, core.scan([str(tree)])


def _raw_payload(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def test_v3_payload_omits_derived_sections(tmp_path):
    _tree, r1 = _scan_tree(tmp_path)
    path = session.save_session(
        r1, r1.scanned_roots, base_dir=str(tmp_path / "data"))
    data = _raw_payload(path)
    assert data["version"] == 3
    assert "class_paths" not in data["state"]
    assert "dir_files" not in data["state"]
    assert "class_size" not in data["state"]
    # решта розділів на місці
    assert "file_meta" in data["state"] and "dir_ok" in data["state"]


def test_v3_load_restores_derived_sections_exactly(tmp_path):
    _tree, r1 = _scan_tree(tmp_path)
    assert any(not files for files in r1.dir_files.values()), \
        "у дереві мусить бути порожня тека"
    path = session.save_session(
        r1, r1.scanned_roots, base_dir=str(tmp_path / "data"))
    r2 = session.load_session(path)
    assert r2.class_size == r1.class_size
    assert r2.class_paths == r1.class_paths
    assert r2.dir_files == r1.dir_files  # включно з порожніми теками
    assert {tuple(sorted(g.paths)) for g in r2.file_groups} == \
           {tuple(sorted(g.paths)) for g in r1.file_groups}
    assert {tuple(sorted(g.paths)) for g in r2.dir_groups} == \
           {tuple(sorted(g.paths)) for g in r1.dir_groups}


def test_v2_legacy_session_still_loads(tmp_path):
    """Синтетичний v2-файл із явними class_paths/dir_files читається."""
    root = str(tmp_path / "root")
    fa, fb = f"{root}/a.bin", f"{root}/b.bin"
    cls = "1000:" + "ab" * 32
    payload = {
        "version": 2,
        "created_ns": 1,
        "roots": [root],
        "state": {
            "file_meta": {fa: [1000, 1, 1], fb: [1000, 1, 1]},
            "file_class": {fa: cls, fb: cls},
            "class_size": {cls: 1000},
            "class_paths": {cls: [fa, fb]},
            "dir_files": {root: [fa, fb]},
            "dir_children": {},
            "dir_links": {},
            "dir_aliases": {},
            "dir_ok": {root: True},
            "ignored_pairs": [],
        },
    }
    path = tmp_path / "legacy.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    r = session.load_session(str(path))
    assert r.class_paths == {cls: [fa, fb]}
    assert r.dir_files == {root: [fa, fb]}
    assert len(r.file_groups) == 1


def test_v2_missing_derived_sections_still_rejected(tmp_path):
    """Строгість для старого формату не послаблено: v2 без class_paths
    при непорожньому class_size — і далі помилка."""
    root = str(tmp_path / "root")
    fa = f"{root}/a.bin"
    cls = "1000:" + "cd" * 32
    payload = {
        "version": 2,
        "created_ns": 1,
        "roots": [root],
        "state": {
            "file_meta": {fa: [1000, 1, 1]},
            "file_class": {fa: cls},
            "class_size": {cls: 1000},
            "dir_children": {},
            "dir_links": {},
            "dir_aliases": {},
            "dir_ok": {root: True},
            "ignored_pairs": [],
        },
    }
    path = tmp_path / "legacy-broken.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    try:
        session.load_session(str(path))
    except ValueError:
        pass
    else:
        raise AssertionError("v2 без class_paths мусить відхилятися")


def test_session_size_budget_27k(tmp_path):
    """Критерій S3: сесія на 27 000 файлів < 0.65 МБ.

    Профіль qa/mem_benchmark.py DUP_RATIO=0.1: ~10% файлів у класах-парах
    (справжні ентропійні digest-и), storage/alloc лише на дубльованих —
    як у реальному скані. На форматі v2 цей профіль давав 827 819 байтів
    (тест падав); v3 без похідних розділів — 631 740.
    """
    res = core.ScanResult()
    root = "/private/tmp/s3-bench"
    res.scanned_roots = [root]
    res.dir_ok[root] = True
    res.dir_files[root] = []
    res.dir_children[root] = []
    n = 27_000
    cls_n = 0
    uniq_n = 0
    for d in range(n // 10):
        dirpath = f"{root}/dir{d:05d}"
        res.dir_children[root].append(dirpath)
        res.dir_ok[dirpath] = True
        res.dir_files[dirpath] = []
        for j in range(10):
            i = d * 10 + j
            p = f"{dirpath}/file{j:04d}.bin"
            size = 4096 + (i % 100) * 512
            res.file_meta[p] = core.FileInfo(
                p, size, 10 ** 18 + i, 10 ** 18, 10 ** 18 + i, 1, i)
            res.dir_files[dirpath].append(p)
            in_pair = (i % 20) in (0, 1)
            if in_pair and i % 20 == 0:
                cls_n += 1
                digest = hashlib.sha256(str(cls_n).encode()).hexdigest()
                cls = f"{size}:{digest}"
                res.file_class[p] = cls
                res.class_size[cls] = size
                res.class_paths[cls] = [p]
                res.file_storage[p] = (16777231, i * 4096)
                res.file_alloc[p] = size
                pending = cls
            elif in_pair:
                prev = res.class_paths[pending][0]
                res.file_meta[p].size = res.file_meta[prev].size
                res.file_class[p] = pending
                res.class_paths[pending].append(p)
                res.file_storage[p] = (16777231, i * 4096)
                res.file_alloc[p] = res.file_meta[p].size
            else:
                uniq_n += 1
                res.file_class[p] = f"u:{uniq_n}"
    res.files_seen = n
    path = session.save_session(res, [root], base_dir=str(tmp_path / "data"))
    assert path
    size = os.path.getsize(path)
    assert size < 0.65 * 1024 * 1024, f"сесія завелика: {size:,} байтів"
