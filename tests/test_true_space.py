"""T1–T3: «звільнить» каже правду — сім'ї сховища з реальним обсягом
(клони, розріджені), клон-чесність тек, персист обліку в сесію."""

import os
import subprocess
import sys
import tempfile

import pytest

os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.session as session  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def clone(src: str, dst: str) -> bool:
    return subprocess.run(["cp", "-c", src, dst], capture_output=True).returncode == 0


def make_sparse(path, logical: int, tail: bytes) -> None:
    """Розріджений файл: дірка + хвіст. Логічний розмір великий,
    зайняте місце — маленьке."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.seek(logical - len(tail))
        fh.write(tail)


# ---- T1: сім'ї сховища з реальним обсягом ----------------------------------


def test_two_real_copies_classic_freeable(tmp_path):
    data = os.urandom(200 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    g = r.file_groups[0]
    assert g.families == 2
    assert g.wasted == pytest.approx(g.size, rel=0.05), (
        "незалежні нестиснуті копії -> класична оцінка"
    )


def test_compressed_copies_report_allocated_not_logical(tmp_path):
    """Обидві копії стиснуті (ditto --hfsCompression): «звільнить» мусить
    показати реально зайняте, а не логічні мегабайти."""
    logical = 4 * 1024 * 1024
    plain = tmp_path / "plain.bin"
    plain.write_bytes(b"A" * logical)
    for top in ("A", "B"):
        d = tmp_path / "t" / top
        d.mkdir(parents=True)
        subprocess.run(
            ["ditto", "--hfsCompression", str(plain), str(d / "c.bin")],
            check=True,
            capture_output=True,
        )
    st = os.lstat(tmp_path / "t/A/c.bin")
    if st.st_blocks * 512 >= logical:
        pytest.skip("ФС не стискає прозоро")
    r = core.scan([str(tmp_path / "t")])
    assert len(r.file_groups) == 1
    g = r.file_groups[0]
    assert g.size == logical, "ідентичність групи — за логічним вмістом"
    assert g.wasted < logical // 8, (
        f"«звільнить» мусить бути ~зайнятим місцем, не 4 МіБ: {g.wasted}"
    )


def test_sparse_copies_report_allocated_not_logical(tmp_path):
    logical = 8 * 1024 * 1024  # 8 МіБ логічно, зайнято ~64 КіБ
    tail = os.urandom(64 * 1024)
    make_sparse(tmp_path / "t/A/s.bin", logical, tail)
    make_sparse(tmp_path / "t/B/s.bin", logical, tail)
    st = os.lstat(tmp_path / "t/A/s.bin")
    if st.st_blocks * 512 >= logical:
        pytest.skip("ФС не робить файли розрідженими")
    r = core.scan([str(tmp_path / "t")])
    assert len(r.file_groups) == 1
    g = r.file_groups[0]
    assert g.size == logical, "ідентичність групи — за логічним вмістом"
    assert g.wasted < logical // 4, (
        f"«звільнить» мусить бути ~зайнятим місцем, не 8 МіБ: {g.wasted}"
    )


def test_unknown_family_conservative(tmp_path, monkeypatch):
    data = os.urandom(150 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    monkeypatch.setattr(core, "_storage_id", lambda p: None)
    r = core.scan([str(tmp_path / "t")])
    g = r.file_groups[0]
    assert g.families == 2
    assert g.wasted == g.size


# ---- T2: клон-чесність тек --------------------------------------------------


def test_cloned_dir_group_wasted_zero(tmp_path):
    src = tmp_path / "t/A"
    make(src / "f1.bin", os.urandom(120 * 1024))
    make(src / "sub/f2.bin", os.urandom(90 * 1024))
    if (
        subprocess.run(
            ["cp", "-cR", str(src), str(tmp_path / "t/B")], capture_output=True
        ).returncode
        != 0
    ):
        pytest.skip("ФС без clonefile")
    r = core.scan([str(tmp_path / "t")])
    assert len(r.dir_groups) == 1
    g = r.dir_groups[0]
    assert g.families == 1, "клонована тека ділить сховище з оригіналом"
    assert g.wasted == 0, "видалення клон-теки не звільняє місця"


def test_copied_dir_group_classic_wasted(tmp_path):
    src = tmp_path / "t/A"
    make(src / "f1.bin", os.urandom(120 * 1024))
    make(src / "sub/f2.bin", os.urandom(90 * 1024))
    subprocess.run(["cp", "-R", str(src), str(tmp_path / "t/B")], check=True)
    r = core.scan([str(tmp_path / "t")])
    assert len(r.dir_groups) == 1
    g = r.dir_groups[0]
    assert g.families == 2
    assert g.wasted == g.size, "справжня копія теки -> класична оцінка"


# ---- T3: персист обліку в сесію --------------------------------------------


def test_session_roundtrip_keeps_honest_freeable(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    make(tmp_path / "t/A/f.bin", os.urandom(160 * 1024))
    (tmp_path / "t/B").mkdir(parents=True)
    if not clone(str(tmp_path / "t/A/f.bin"), str(tmp_path / "t/B/f.bin")):
        pytest.skip("ФС без clonefile")
    r = core.scan([str(tmp_path / "t")])
    assert r.file_groups[0].wasted == 0
    p = session.save_session(r, [str(tmp_path / "t")])
    loaded = session.load_session(p)
    assert loaded.file_storage, "сесія мусить нести клон-облік"
    assert loaded.file_groups[0].families == 1
    assert loaded.file_groups[0].wasted == 0, (
        "історична сесія показує ту саму чесну оцінку, що й live"
    )


def test_legacy_session_without_storage_stays_classic(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(tmp_path / "data"))
    data = os.urandom(140 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    r = core.scan([str(tmp_path / "t")])
    r.file_storage = {}
    r.file_alloc = {}
    p = session.save_session(r, [str(tmp_path / "t")])
    loaded = session.load_session(p)
    g = loaded.file_groups[0]
    assert g.families == 2 and g.wasted == g.size
