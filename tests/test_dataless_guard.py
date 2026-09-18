"""B1: iCloud dataless-файли ніколи не читаються (нуль викачувань з хмари),
тека з ними — недоказова."""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_dataless_skipped_and_dir_unprovable(tmp_path, monkeypatch):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 4096)
    cloud = str(tmp_path / "A/cloud.bin")
    make(tmp_path / "A/cloud.bin", b"C" * 2048)

    real = core._is_dataless
    monkeypatch.setattr(core, "_is_dataless", lambda st, _r=real: st.st_size == 2048)

    opened: list[str] = []
    real_hash = core._hash_file

    def spy(path, size, full, cancel, pause=None, *a, **k):
        opened.append(path)
        return real_hash(path, size, full, cancel, pause, *a, **k)

    monkeypatch.setattr(core, "_hash_file", spy)
    r = core.scan([str(tmp_path)])
    assert cloud not in opened, "dataless-файл НЕ читається (нуль викачувань)"
    assert cloud not in r.file_meta
    assert r.dataless_skipped == 1
    assert any("iCloud" in e for e in r.errors), "пояснення користувачу є"
    # тека A недоказова -> пара A/B не може бути групою тек
    names = [{os.path.basename(p) for p in g.paths} for g in r.dir_groups]
    assert {"A", "B"} not in names


def test_no_dataless_zero_behavior_change(tmp_path):
    for top in ("A", "B"):
        make(tmp_path / top / "x.bin", b"X" * 4096)
    r = core.scan([str(tmp_path)])
    assert r.dataless_skipped == 0
    assert len(r.file_groups) == 1
    names = [{os.path.basename(p) for p in g.paths} for g in r.dir_groups]
    assert {"A", "B"} in names


def test_is_dataless_flag_math():
    class St:
        st_flags = 0x40000000

    class St2:
        st_flags = 0

    class St3:
        pass  # немає st_flags (не-macOS шлях)

    assert core._is_dataless(St()) is True
    assert core._is_dataless(St2()) is False
    assert core._is_dataless(St3()) is False
