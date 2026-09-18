"""Злиття не ламається на файлах, яких скан не охопив.

Дзеркало реального сценарію: профіль із min_size=1 МіБ, тека-копія з
одним унікальним великим файлом і багатьма малими. Малі файли без класу
дублікатів не повинні блокувати крок Кошика («N файлів без живої копії»).
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.preferences as preferences  # noqa: E402

MIB = 1024 * 1024


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def owner_case(tmp_path):
    """A і B: спільний великий файл, у B один унікальний великий і 12 малих."""
    shared = os.urandom(2 * MIB)
    make(tmp_path / "t/A/shared.bin", shared)
    make(tmp_path / "t/B/shared.bin", shared)
    make(tmp_path / "t/B/only-big.bin", os.urandom(3 * MIB))
    for i in range(12):
        make(tmp_path / "t/B/small" / f"s{i}.bin", os.urandom(4096))
    profile = preferences.ScanProfile(name="1MiB", min_size=MIB)
    return profile


def test_uncovered_files_have_no_class(tmp_path):
    """Базовий факт: профіль справді лишає малі файли без класу."""
    profile = owner_case(tmp_path)
    r = core.scan([str(tmp_path / "t")], profile=profile)
    small = str(tmp_path / "t/B/small/s0.bin")
    assert small not in r.file_class, "малий файл не мусить мати класу"
    assert core.proof_kind(r, small) is None


def test_merge_plan_includes_uncovered_and_labels_them(tmp_path):
    profile = owner_case(tmp_path)
    r = core.scan([str(tmp_path / "t")], profile=profile)
    plan, total, summary = core.merge_plan(
        r, str(tmp_path / "t/B"), str(tmp_path / "t/A"), with_summary=True
    )
    rels = {rel for _s, _p, rel in plan}
    assert "only-big.bin" in rels, "унікальний за вмістом мусить бути в плані"
    for i in range(12):
        assert os.path.join("small", f"s{i}.bin") in rels, (
            "неохоплений профілем файл мусить переноситись, а не лишатись"
        )
    assert "shared.bin" not in rels, "доказаний дублікат лишається в джерелі"
    assert summary["unique"] == 1
    assert summary["uncovered"] == 12
    assert total > 3 * MIB


def test_merge_completes_and_source_goes_to_trash(tmp_path, monkeypatch):
    import dupscan.ui.app as app_mod

    profile = owner_case(tmp_path)
    r = core.scan([str(tmp_path / "t")], profile=profile)
    src = str(tmp_path / "t/B")
    dst = str(tmp_path / "t/A")
    plan, _total, _summary = core.merge_plan(r, src, dst, with_summary=True)
    moved, errors = app_mod._move_files(r, plan, dst, src_dir=src)
    assert errors == [], f"перенос не мусив падати: {errors}"
    assert len(moved) == 13, f"перенесено {len(moved)} замість 13"
    trashed: list[str] = []
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    kind, payload = app_mod._verify_then_trash_dir(r, src)
    assert kind == "ok", f"після переносу джерело мусить піти в Кошик, а отримали {kind}/{payload}"
    assert trashed == [src]
    assert os.path.exists(os.path.join(dst, "only-big.bin"))
    assert os.path.exists(os.path.join(dst, "small", "s0.bin"))


def test_collision_gets_suffix_not_overwrite(tmp_path):
    import dupscan.ui.app as app_mod

    profile = owner_case(tmp_path)
    original = os.urandom(5000)
    make(tmp_path / "t/A/small/s0.bin", original)  # колізія імені
    r = core.scan([str(tmp_path / "t")], profile=profile)
    src = str(tmp_path / "t/B")
    dst = str(tmp_path / "t/A")
    plan, _t, _s = core.merge_plan(r, src, dst, with_summary=True)
    moved, errors = app_mod._move_files(r, plan, dst, src_dir=src)
    assert errors == []
    assert (tmp_path / "t/A/small/s0.bin").read_bytes() == original, (
        "наявний файл не мусить бути перезаписаний"
    )
    assert any("s0 (2).bin" in dst_path for _src, dst_path in moved), (
        f"колізія мусить дати « (2)»: {[d for _s, d in moved]}"
    )
