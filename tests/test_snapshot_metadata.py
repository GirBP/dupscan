"""F2: знімок теки — лише метадані, нуль читань вмісту.

Вміст доводять пофайлові докази (F1). Знімок ловить будь-яку зміну структури
або метаданих під час операції: додано, видалено, замінено, відредаговано.
"""

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402


def make(p, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def tree(tmp_path):
    make(tmp_path / "d/a.bin", os.urandom(300 * 1024))
    make(tmp_path / "d/sub/b.bin", os.urandom(200 * 1024))
    return str(tmp_path / "d")


def test_snapshot_reads_zero_content_bytes(tmp_path, monkeypatch):
    read = {"bytes": 0}
    real = core._hash_file

    def spy(path, size, full, cancel, pause=None, *a, **k):
        read["bytes"] += size if full else min(core.PARTIAL, size)
        return real(path, size, full, cancel, pause, *a, **k)

    monkeypatch.setattr(core, "_hash_file", spy)
    d = tree(tmp_path)
    snap = core.snapshot_directory_state(d)
    assert snap is not None
    assert read["bytes"] == 0, f"знімок теки мусить бути метаданим, а прочитано {read['bytes']} Б"


def test_snapshot_stable_when_nothing_changes(tmp_path):
    d = tree(tmp_path)
    assert core.snapshot_directory_state(d) == core.snapshot_directory_state(d)


def test_snapshot_detects_added_file(tmp_path):
    d = tree(tmp_path)
    before = core.snapshot_directory_state(d)
    make(tmp_path / "d/new.bin", b"N" * 1000)
    assert core.snapshot_directory_state(d) != before


def test_snapshot_detects_removed_file(tmp_path):
    d = tree(tmp_path)
    before = core.snapshot_directory_state(d)
    os.remove(tmp_path / "d/a.bin")
    assert core.snapshot_directory_state(d) != before


def test_snapshot_detects_replaced_file_same_size(tmp_path):
    d = tree(tmp_path)
    before = core.snapshot_directory_state(d)
    time.sleep(0.01)
    (tmp_path / "d/a.bin").write_bytes(os.urandom(300 * 1024))
    assert core.snapshot_directory_state(d) != before, (
        "заміна вмісту тим самим розміром мусить змінити знімок"
    )


def test_snapshot_detects_edited_file(tmp_path):
    d = tree(tmp_path)
    before = core.snapshot_directory_state(d)
    time.sleep(0.01)
    with open(tmp_path / "d/sub/b.bin", "r+b") as fh:
        fh.seek(0)
        fh.write(b"XXXX")
    assert core.snapshot_directory_state(d) != before


def test_snapshot_detects_renamed_file(tmp_path):
    d = tree(tmp_path)
    before = core.snapshot_directory_state(d)
    os.rename(tmp_path / "d/a.bin", tmp_path / "d/renamed.bin")
    assert core.snapshot_directory_state(d) != before


def test_snapshot_includes_symlink_target(tmp_path):
    d = tree(tmp_path)
    os.symlink("/tmp/one", tmp_path / "d/link")
    before = core.snapshot_directory_state(d)
    os.remove(tmp_path / "d/link")
    os.symlink("/tmp/two", tmp_path / "d/link")
    assert core.snapshot_directory_state(d) != before, "symlink входить у знімок як ціль"


def test_symlink_does_not_block_directory_trash(tmp_path, monkeypatch):
    """Symlink більше не робить теку автоматично небезпечною."""
    import dupscan.ui.app as app_mod

    data = os.urandom(150 * 1024)
    make(tmp_path / "t/A/f.bin", data)
    make(tmp_path / "t/B/f.bin", data)
    os.symlink("/tmp/target", tmp_path / "t/B/link")
    r = core.scan([str(tmp_path / "t")])
    trashed: list[str] = []
    monkeypatch.setattr(app_mod, "to_trash", lambda ps, **k: (trashed.extend(ps), [])[1])
    kind, payload = app_mod._verify_then_trash_dir(r, str(tmp_path / "t/B"))
    assert kind == "ok", f"тека мусила піти в Кошик, а отримали {kind}/{payload}"
    assert trashed == [str(tmp_path / "t/B")]
