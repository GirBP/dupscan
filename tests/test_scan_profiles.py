import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.domain.core as core  # noqa: E402
import dupscan.infra.preferences as preferences  # noqa: E402


def test_profile_filters_size_extensions_hidden_and_paths(tmp_path):
    skip = tmp_path / "skip"
    skip.mkdir()
    (tmp_path / "small.jpg").write_bytes(b"x")
    (tmp_path / "large.jpg").write_bytes(b"same" * 100)
    (tmp_path / "large-copy.jpg").write_bytes(b"same" * 100)
    (tmp_path / "note.txt").write_bytes(b"same" * 100)
    (tmp_path / ".hidden.jpg").write_bytes(b"same" * 100)
    (skip / "ignored.jpg").write_bytes(b"same" * 100)
    profile = preferences.ScanProfile(
        "Photos", min_size=10, include_extensions=("jpg",),
        excluded_paths=(str(skip),), include_hidden=False,
    )
    result = core.scan([str(tmp_path)], profile=profile)
    assert result.files_seen == 2
    assert len(result.file_groups) == 1
    assert {os.path.basename(path) for path in result.file_groups[0].paths} == {
        "large.jpg", "large-copy.jpg"
    }
    # Exact folder duplicate claims are unavailable when content was filtered.
    assert not result.dir_groups


def test_default_scan_keeps_legacy_symlink_manifest(tmp_path):
    for name in ("A", "B"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "file").write_bytes(b"same")
        (directory / "link").symlink_to("file")
    legacy = core.scan([str(tmp_path)])
    profiled = core.scan([str(tmp_path)], profile=preferences.DEFAULT_PROFILE)
    assert legacy.dir_groups
    assert not profiled.dir_groups


def test_profile_filters_have_parallel_walk_parity(tmp_path):
    for name in ("A", "B"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "same.jpg").write_bytes(b"same" * 100)
        (directory / ".hidden.jpg").write_bytes(b"hidden" * 100)
        (directory / "link").symlink_to("same.jpg")
    profile = preferences.ScanProfile(
        "Visible photos", include_extensions=("jpg",), include_hidden=False,
        include_symlinks=False)
    sequential = core.scan([str(tmp_path)], profile=profile, walk_threads=1)
    parallel = core.scan([str(tmp_path)], profile=profile, walk_threads=4)
    assert sequential.files_seen == parallel.files_seen == 2
    assert [group.paths for group in sequential.file_groups] == [
        group.paths for group in parallel.file_groups]
    assert sequential.dir_groups == parallel.dir_groups == []
