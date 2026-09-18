"""B2: «звільнить» не бреше на APFS-клонах. families = незалежні сховища."""

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def clone(src: str, dst: str) -> bool:
    return subprocess.run(["cp", "-c", src, dst], capture_output=True).returncode == 0


def clones_supported(tmp_path) -> bool:
    a = tmp_path / "___probe_src.bin"
    a.write_bytes(b"x" * 4096)
    ok = clone(str(a), str(tmp_path / "___probe_dst.bin"))
    return ok


def test_storage_id_clone_vs_copy(tmp_path):
    if not clones_supported(tmp_path):
        pytest.skip("ФС без clonefile")
    make(tmp_path / "orig.bin", os.urandom(256 * 1024))
    assert clone(str(tmp_path / "orig.bin"), str(tmp_path / "clone.bin"))
    shutil.copyfile(tmp_path / "orig.bin", tmp_path / "copy.bin")
    so = core._storage_id(str(tmp_path / "orig.bin"))
    sc = core._storage_id(str(tmp_path / "clone.bin"))
    sk = core._storage_id(str(tmp_path / "copy.bin"))
    assert so is not None and sc is not None and sk is not None
    assert so == sc, "клон ділить фізичне сховище з оригіналом"
    assert so != sk, "справжня копія має власні екстенти"


def test_pure_clone_group_wasted_zero(tmp_path):
    if not clones_supported(tmp_path):
        pytest.skip("ФС без clonefile")
    make(tmp_path / "t/A/f.bin", os.urandom(200 * 1024))
    (tmp_path / "t/B").mkdir(parents=True)
    assert clone(str(tmp_path / "t/A/f.bin"), str(tmp_path / "t/B/f.bin"))
    r = core.scan([str(tmp_path / "t")])
    assert len(r.file_groups) == 1
    g = r.file_groups[0]
    assert g.families == 1, "два клони = одне фізичне сховище"
    assert g.wasted == 0, "видалення клона не звільняє нічого — не обіцяти"


def test_mixed_clone_and_copy_wasted_one_size(tmp_path):
    if not clones_supported(tmp_path):
        pytest.skip("ФС без clonefile")
    data = os.urandom(180 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    (tmp_path / "t/B").mkdir(parents=True)
    assert clone(str(tmp_path / "t/A/f.bin"), str(tmp_path / "t/B/f.bin"))
    make(tmp_path / "t/C/f.bin", data)  # справжня копія (окремі блоки)
    r = core.scan([str(tmp_path / "t")])
    assert len(r.file_groups) == 1
    g = r.file_groups[0]
    assert len(g.paths) == 3 and g.families == 2
    assert g.wasted == g.size, "звільнити реально можна лише одну копію"


def test_probe_failure_falls_back_to_classic(tmp_path, monkeypatch):
    data = os.urandom(150 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    monkeypatch.setattr(core, "_storage_id", lambda p: None)
    r = core.scan([str(tmp_path / "t")])
    g = r.file_groups[0]
    assert g.families == 2, "невідоме сховище = консервативно окремі копії"
    assert g.wasted == g.size


def test_families_survive_recompute(tmp_path):
    if not clones_supported(tmp_path):
        pytest.skip("ФС без clonefile")
    data = os.urandom(160 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    (tmp_path / "t/B").mkdir(parents=True)
    assert clone(str(tmp_path / "t/A/f.bin"), str(tmp_path / "t/B/f.bin"))
    make(tmp_path / "t/C/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    victim = str(tmp_path / "t/C/f.bin")
    os.remove(victim)
    core.recompute(r, {victim})
    assert len(r.file_groups) == 1
    g = r.file_groups[0]
    assert g.families == 1 and g.wasted == 0, (
        "після зникнення справжньої копії лишились самі клони — звільняти нічого"
    )
