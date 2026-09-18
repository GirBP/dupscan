"""Злиття подібних тек: перевірка і перенос symlink-ів у джерелі, в обох
режимах (move/copy).

merge_plan() кладе symlink-и в окрему "symlinks" категорію (size=0).
core.verify_current_file() читає ВМІСТ через open(path) — для symlink-шляху
це йде за посиланням і хешує ЦІЛЬОВИЙ файл, а звіряє прочитане з
lstat-розміром самого лінка (довжина тексту цілі, майже завжди інша за
розмір цілі) — гарантована невідповідність, ESTALE. Тому
MergePreparationWorker.run() і fsops._transfer_files() гілкують на
os.path.islink(src): для symlink кличуть core.verify_current_symlink()
(digest, stat за ТЕКСТОМ цілі — той самий контракт, що й у
core.snapshot_directory()), а не verify_current_file().

У move-гілці _transfer_files гейт типу поточного джерела перед
публікацією звіряє з ОЧІКУВАНИМ типом: symlink лишається symlink, а не
завжди вимагає S_ISREG.
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
from dupscan.infra.fsops import _copy_files, _move_files  # noqa: E402


def make(p, data: bytes) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_verify_current_symlink_gives_a_stable_target_digest(tmp_path):
    target = tmp_path / "target.bin"
    make(target, os.urandom(5000))
    link = tmp_path / "link"
    os.symlink("target.bin", link)

    digest, st = core.verify_current_symlink(str(link))
    assert digest
    # Стабільний за незмінної цілі:
    digest2, _st2 = core.verify_current_symlink(str(link))
    assert digest2 == digest
    # Інша ціль -> інший digest (fail-closed на зміні):
    os.remove(link)
    os.symlink("target.bin.other", link)
    digest3, _st3 = core.verify_current_symlink(str(link))
    assert digest3 != digest


def test_verify_current_file_cannot_be_used_directly_on_a_symlink(tmp_path):
    """Документує САМ дефект: verify_current_file лишається зумисно
    строгим для symlink-шляхів (не підмінює контракт мовчки) — тому
    виклики повинні гілкувати на verify_current_symlink, а не покладатися
    на verify_current_file."""
    target = tmp_path / "target.bin"
    make(target, os.urandom(5000))
    link = tmp_path / "link"
    os.symlink("target.bin", link)

    import pytest
    with pytest.raises(OSError):
        core.verify_current_file(str(link))


def _plan_with_symlink(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    make(src / "keep.bin", os.urandom(4000))
    link = src / "link"
    os.symlink("keep.bin", link)
    res = core.ScanResult()
    plan = [(0, str(link), "link")]
    return res, plan, str(src), str(dst)


def test_move_transfer_handles_a_symlink_plan_entry(tmp_path):
    res, plan, src, dst = _plan_with_symlink(tmp_path)
    os.makedirs(dst, exist_ok=True)

    completed, errors = _move_files(res, plan, dst, src)

    assert errors == [], f"move symlink не мав падати: {errors}"
    assert completed == [(os.path.join(src, "link"), os.path.join(dst, "link"))]
    assert os.path.islink(os.path.join(dst, "link"))
    assert os.readlink(os.path.join(dst, "link")) == "keep.bin"
    assert not os.path.exists(os.path.join(src, "link"))  # move прибрав джерело


def test_copy_transfer_handles_a_symlink_plan_entry(tmp_path):
    res, plan, src, dst = _plan_with_symlink(tmp_path)
    os.makedirs(dst, exist_ok=True)
    expected_digests = {
        str(tmp_path / "src" / "link"): core.verify_current_symlink(
            str(tmp_path / "src" / "link"))[0]
    }

    completed, errors = _copy_files(
        res, plan, dst, src, expected_digests=expected_digests)

    assert errors == [], f"copy symlink не мав падати: {errors}"
    assert os.path.islink(os.path.join(dst, "link"))
    assert os.readlink(os.path.join(dst, "link")) == "keep.bin"
    assert os.path.islink(os.path.join(src, "link"))  # copy джерела не чіпає
