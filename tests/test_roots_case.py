"""T4: корені дедуплікуються за (dev, ino), не за рядком шляху.
На case-insensitive APFS «X» і «x» — та сама тека; хибних дублікатів нема."""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def case_insensitive_here(tmp_path) -> bool:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    return (tmp_path / "caseprobe").is_dir()


def test_same_dir_different_case_roots_scanned_once(tmp_path):
    if not case_insensitive_here(tmp_path):
        pytest.skip("ФС регістрочутлива — сценарій не відтворюється")
    make(tmp_path / "Dir/unique.bin", b"U" * 5000)
    r = core.scan([str(tmp_path / "Dir"), str(tmp_path / "dir")])
    assert r.files_seen == 1, "та сама тека двома регістрами — один скан"
    assert r.file_groups == [], "файл не має стати дублікатом самого себе"


def test_same_dir_symlinked_root_scanned_once(tmp_path):
    make(tmp_path / "Real/unique.bin", b"U" * 5000)
    os.symlink(tmp_path / "Real", tmp_path / "Alias")
    r = core.scan([str(tmp_path / "Real"), str(tmp_path / "Alias")])
    assert r.files_seen == 1
    assert r.file_groups == []


def test_distinct_dirs_both_scanned(tmp_path):
    make(tmp_path / "One/a.bin", b"A" * 3000)
    make(tmp_path / "Two/b.bin", b"B" * 4000)
    r = core.scan([str(tmp_path / "One"), str(tmp_path / "Two")])
    assert r.files_seen == 2
