"""Компактне представлення результату в пам'яті.

FileInfo — slots-датаклас (без __dict__ на кожен із сотень тисяч
об'єктів); після load_session рядки-класи та рядки-шляхи спільні
(один об'єкт на значення), а не окрема копія на кожен запис JSON.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_fileinfo_has_slots_layout():
    info = core.FileInfo("/tmp/x", 1, 2, 3)
    assert not hasattr(info, "__dict__")
    assert info.size == 1 and info.ctime_ns == 0


def _round_trip(tmp_path):
    tree = tmp_path / "tree"
    for top in ("A", "B"):
        make(tree / top / "x.bin", b"X" * 1000)
        make(tree / top / "y.bin", b"Y" * 2000)
    r1 = core.scan([str(tree)])
    path = session.save_session(
        r1, r1.scanned_roots, base_dir=str(tmp_path / "data"))
    assert path
    return session.load_session(path)


def test_loaded_session_shares_class_strings(tmp_path):
    r2 = _round_trip(tmp_path)
    by_class: dict[str, list[str]] = {}
    for p, cls in r2.file_class.items():
        by_class.setdefault(cls, []).append(p)
    dup_classes = {c: ps for c, ps in by_class.items() if len(ps) >= 2}
    assert dup_classes
    for cls, paths in dup_classes.items():
        objs = {id(r2.file_class[p]) for p in paths}
        assert len(objs) == 1, "клас вмісту мусить бути спільним об'єктом"
        # ключі class_size/class_paths — той самий об'єкт, не копія
        assert any(k is r2.file_class[paths[0]] for k in r2.class_size)
        assert any(k is r2.file_class[paths[0]] for k in r2.class_paths)


def test_loaded_session_shares_path_strings(tmp_path):
    r2 = _round_trip(tmp_path)
    assert r2.file_meta
    for p, info in r2.file_meta.items():
        assert info.path is p, "FileInfo.path мусить бути ключем file_meta"
    for p in r2.file_storage:
        assert any(k is p for k in (p,)) and p in r2.file_meta
        # об'єкт ключа у file_storage — той самий, що у file_meta
    meta_ids = {id(k) for k in r2.file_meta}
    assert all(id(p) in meta_ids for p in r2.file_storage), \
        "file_storage не має тримати копії шляхів"
    assert all(id(p) in meta_ids for p in r2.file_alloc), \
        "file_alloc не має тримати копії шляхів"
    for paths in r2.class_paths.values():
        assert all(id(p) in meta_ids for p in paths), \
            "class_paths не має тримати копії шляхів"
    for paths in r2.dir_files.values():
        assert all(id(p) in meta_ids for p in paths), \
            "dir_files не має тримати копії шляхів"
