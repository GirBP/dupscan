"""B3: мережевий корінь -> advisory (видиме попередження), нуль змін поведінки."""

import os
import sys
import tempfile

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_real_root_fs_type_detected():
    t = core._volume_fs_type("/")
    assert isinstance(t, str) and t, "корінь системи мусить мати тип ФС"
    assert t not in core._NETWORK_FS


def test_network_root_gets_advisory(tmp_path, monkeypatch):
    make(tmp_path / "a.bin", b"A" * 4096)
    monkeypatch.setattr(core, "_volume_fs_type", lambda p: "smbfs")
    r = core.scan([str(tmp_path)])
    assert any("мережевий том" in a for a in r.advisories)
    assert any("мережевий том" in e for e in r.errors), "видимість у наявному UI"
    assert r.files_seen == 1, "advisory не змінює поведінку скану"


def test_local_root_no_advisory(tmp_path):
    make(tmp_path / "a.bin", b"A" * 4096)
    r = core.scan([str(tmp_path)])
    assert r.advisories == []


def test_fs_type_failure_is_silent(tmp_path, monkeypatch):
    make(tmp_path / "a.bin", b"A" * 4096)
    monkeypatch.setattr(core, "_volume_fs_type", lambda p: "")
    r = core.scan([str(tmp_path)])
    assert r.advisories == [] and r.files_seen == 1
