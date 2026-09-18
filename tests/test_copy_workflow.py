import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.ui.app as app  # noqa: E402
import dupscan.domain.core as core  # noqa: E402


def test_verified_copy_keeps_source_and_never_overwrites(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "shared.bin").write_bytes(b"shared" * 100)
    (target / "shared-copy.bin").write_bytes(b"shared" * 100)
    unique = source / "unique.txt"
    unique.write_bytes(b"unique-content")
    existing = target / "unique.txt"
    existing.write_bytes(b"do-not-overwrite")
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))

    copied, errors = app._copy_files(result, plan, str(target))

    assert errors == []
    assert unique.read_bytes() == b"unique-content"
    assert existing.read_bytes() == b"do-not-overwrite"
    destinations = [destination for _source, destination in copied]
    assert destinations == [str(target / "unique (2).txt")]
    assert (target / "unique (2).txt").read_bytes() == b"unique-content"


def test_verified_copy_rejects_source_changed_after_scan(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    path = source / "unique.bin"
    path.write_bytes(b"before")
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    path.write_bytes(b"after!")

    copied, errors = app._copy_files(result, plan, str(target))

    assert copied == []
    assert errors
    assert not (target / "unique.bin").exists()


def test_merge_helpers_reject_plan_source_outside_declared_root(tmp_path):
    source, target, outside_dir = (
        tmp_path / "source", tmp_path / "target", tmp_path / "outside"
    )
    source.mkdir()
    target.mkdir()
    outside_dir.mkdir()
    outside = outside_dir / "secret.bin"
    outside.write_bytes(b"secret")
    result = core.scan([str(source), str(target), str(outside_dir)])
    plan = [(outside.stat().st_size, str(outside), "secret.bin")]

    for helper in (app._copy_files, app._move_files):
        completed, errors = helper(result, plan, str(target), str(source))
        assert completed == []
        assert errors
        assert outside.read_bytes() == b"secret"
        assert not (target / "secret.bin").exists()


def test_verified_move_retries_collision_without_overwriting(tmp_path, monkeypatch):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "unique.txt"
    unique.write_bytes(b"unique-content")
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    desired = target / "unique.txt"
    real_link = os.link

    def competing_link(
            src, dst, *, follow_symlinks=True,
            src_dir_fd=None, dst_dir_fd=None):
        if os.fspath(dst) == desired.name and not desired.exists():
            desired.write_bytes(b"concurrent-content")
        return real_link(
            src, dst, follow_symlinks=follow_symlinks,
            src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(app.os, "link", competing_link)
    moved, errors = app._move_files(result, plan, str(target), str(source))

    assert errors == []
    assert moved == [(str(unique), str(target / "unique (2).txt"))]
    assert not unique.exists()
    assert desired.read_bytes() == b"concurrent-content"
    assert (target / "unique (2).txt").read_bytes() == b"unique-content"


def test_unique_source_must_match_fresh_merge_baseline(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "unique.bin"
    unique.write_bytes(b"AAAA")
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    baseline, _stat = core.verify_current_file(
        str(unique), result.file_meta[str(unique)])

    unique.write_bytes(b"BBBB")
    current = os.stat(unique)
    result.file_meta[str(unique)] = core.FileInfo(
        str(unique), current.st_size, current.st_mtime_ns,
        getattr(current, "st_birthtime_ns", current.st_mtime_ns),
        current.st_ctime_ns, current.st_dev, current.st_ino)

    moved, errors = app._move_files(
        result, plan, str(target), str(source), {str(unique): baseline})

    assert moved == []
    assert errors and "змінився" in errors[0]
    assert unique.read_bytes() == b"BBBB"
    assert not (target / "unique.bin").exists()


def test_destination_symlink_component_is_rejected_without_outside_write(
        tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    (source / "escape").mkdir(parents=True)
    target.mkdir()
    outside.mkdir()
    unique = source / "escape" / "unique.bin"
    unique.write_bytes(b"safe")
    os.symlink(outside, target / "escape")
    result = core.scan([str(source), str(target), str(outside)])
    plan, _total = core.merge_plan(result, str(source), str(target))

    for helper in (app._copy_files, app._move_files):
        completed, errors = helper(
            result, plan, str(target), str(source))
        assert completed == []
        assert errors
        assert unique.read_bytes() == b"safe"
        assert not (outside / "unique.bin").exists()


def test_replaced_source_root_is_rejected_before_copy_or_move(tmp_path):
    source = tmp_path / "source"
    original = tmp_path / "source-original"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "unique.bin"
    unique.write_bytes(b"verified-content")
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    identities = {
        str(source): app._directory_identity(str(source)),
        str(target): app._directory_identity(str(target)),
    }
    source.rename(original)
    source.mkdir()
    (source / "unique.bin").write_bytes(b"replacement-content")

    for helper in (app._copy_files, app._move_files):
        completed, errors = helper(
            result, plan, str(target), str(source),
            root_identities=identities)
        assert completed == []
        assert errors and "замінено" in errors[0]
        assert (source / "unique.bin").read_bytes() == b"replacement-content"
        assert (original / "unique.bin").read_bytes() == b"verified-content"
        assert not (target / "unique.bin").exists()


def test_source_symlink_component_is_never_followed(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    nested = source / "nested"
    nested.mkdir(parents=True)
    target.mkdir()
    outside.mkdir()
    unique = nested / "unique.bin"
    unique.write_bytes(b"verified")
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    nested.rename(source / "nested-original")
    (outside / "unique.bin").write_bytes(b"outside")
    os.symlink(outside, nested)

    for helper in (app._copy_files, app._move_files):
        completed, errors = helper(
            result, plan, str(target), str(source))
        assert completed == []
        assert errors
        assert (outside / "unique.bin").read_bytes() == b"outside"
        assert (source / "nested-original/unique.bin").read_bytes() == b"verified"
        assert not (target / "nested/unique.bin").exists()


def test_cancelled_copy_removes_private_temp_and_keeps_source(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    unique = source / "large.bin"
    unique.write_bytes(b"L" * (3 * 1024 * 1024))
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    cancel = threading.Event()

    def progress(phase, _done, _total):
        if phase.startswith("Копіюю"):
            cancel.set()

    copied, errors = app._copy_files(
        result, plan, str(target), str(source),
        cancel=cancel, progress=progress)

    assert copied == [] and errors == []
    assert unique.exists()
    assert not list(target.rglob(".dupscan-copy-*"))
    assert not (target / "large.bin").exists()


def test_cancelled_move_stops_between_atomic_files(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    for name in ("one.bin", "two.bin"):
        (source / name).write_bytes(name.encode() * 100)
    result = core.scan([str(source), str(target)])
    plan, _total = core.merge_plan(result, str(source), str(target))
    cancel = threading.Event()

    def progress(phase, _done, _total):
        if phase.startswith("Перенесено"):
            cancel.set()

    moved, errors = app._move_files(
        result, plan, str(target), str(source),
        cancel=cancel, progress=progress)

    assert errors == []
    assert len(moved) == 1
    assert len(list(source.glob("*.bin"))) == 1
    assert len(list(target.glob("*.bin"))) == 1
