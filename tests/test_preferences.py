import json
import os
import stat
import sys
from dataclasses import FrozenInstanceError, dataclass

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.preferences as preferences  # noqa: E402


def test_default_profile_preserves_existing_scan_semantics():
    profile = preferences.DEFAULT_PROFILE
    assert profile.name == "Default"
    assert profile.min_size == 0
    assert profile.include_extensions == ()
    assert profile.excluded_extensions == ()
    assert profile.excluded_paths == ()
    assert profile.include_hidden is True
    assert profile.include_bundles is True
    assert profile.include_symlinks is False


def test_profile_normalizes_extensions_paths_and_is_immutable(tmp_path):
    excluded = tmp_path / "skip" / ".." / "private"
    profile = preferences.ScanProfile(
        name="  Photos  ",
        min_size=1024,
        include_extensions=("JPG", ".png", ".JPG"),
        excluded_extensions=("tmp",),
        excluded_paths=(str(excluded), str(tmp_path / "private")),
        include_hidden=False,
    )
    assert profile.name == "Photos"
    assert profile.include_extensions == (".jpg", ".png")
    assert profile.excluded_extensions == (".tmp",)
    assert profile.excluded_paths == (str(tmp_path / "private"),)
    with pytest.raises(FrozenInstanceError):
        profile.min_size = 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "bad/name"},
        {"name": "ok", "min_size": -1},
        {"name": "ok", "min_size": True},
        {"name": "ok", "include_extensions": ("x/y",)},
        {"name": "ok", "excluded_paths": ("relative/path",)},
        {
            "name": "ok",
            "include_extensions": ("jpg",),
            "excluded_extensions": (".JPG",),
        },
        {"name": "ok", "include_hidden": 1},
    ],
)
def test_profile_rejects_unsafe_or_ambiguous_values(kwargs):
    with pytest.raises(ValueError):
        preferences.ScanProfile(**kwargs)


def test_should_include_applies_all_scan_filters(tmp_path):
    excluded = tmp_path / "private"
    profile = preferences.ScanProfile(
        name="Images",
        min_size=100,
        include_extensions=("jpg", ".tar.gz"),
        excluded_extensions=(".thumb.jpg",),
        excluded_paths=(str(excluded),),
        include_hidden=False,
        include_bundles=False,
        include_symlinks=False,
    )
    include = preferences.should_include
    assert include(str(tmp_path / "photo.JPG"), size=100, profile=profile)
    assert include(str(tmp_path / "backup.TAR.GZ"), size=500, profile=profile)
    assert not include(str(tmp_path / "small.jpg"), size=99, profile=profile)
    assert not include(str(tmp_path / "note.txt"), size=500, profile=profile)
    assert not include(str(tmp_path / "x.thumb.jpg"), size=500, profile=profile)
    assert not include(str(excluded / "photo.jpg"), size=500, profile=profile)
    assert not include(str(tmp_path / ".hidden.jpg"), size=500, profile=profile)
    assert not include(
        str(tmp_path / "Thing.app"), size=500, is_bundle=True, profile=profile
    )
    assert not include(
        str(tmp_path / "photo.jpg"), size=500, is_symlink=True, profile=profile
    )


def test_excluded_path_matching_observes_component_boundaries(tmp_path):
    profile = preferences.ScanProfile(
        name="Boundary", excluded_paths=(str(tmp_path / "skip"),)
    )
    assert not preferences.should_include(
        str(tmp_path / "skip" / "a.bin"), size=1, profile=profile
    )
    assert preferences.should_include(
        str(tmp_path / "skipper" / "a.bin"), size=1, profile=profile
    )


def test_profile_round_trip_uses_dupscan_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "app-data"
    monkeypatch.setenv("DUPSCAN_DATA_DIR", str(data_dir))
    profile = preferences.ScanProfile(
        name="Large videos",
        min_size=10_000,
        include_extensions=("mov", "mp4"),
        include_hidden=False,
    )
    preferences.save_profile(profile)

    path = data_dir / "preferences.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    assert preferences.load_profile("large VIDEOS") == profile
    assert [item.name for item in preferences.list_profiles()] == [
        "Default",
        "Large videos",
    ]


def test_save_replaces_profile_case_insensitively_and_keeps_sort_order(tmp_path):
    preferences.save_profile(preferences.ScanProfile("zeta"), tmp_path)
    preferences.save_profile(preferences.ScanProfile("Alpha", min_size=1), tmp_path)
    replacement = preferences.ScanProfile("alpha", min_size=99)
    preferences.save_profile(replacement, tmp_path)

    profiles = preferences.list_profiles(tmp_path)
    assert [profile.name for profile in profiles] == ["Default", "alpha", "zeta"]
    assert preferences.load_profile("ALPHA", tmp_path).min_size == 99


def test_delete_profile_is_persistent_and_default_is_protected(tmp_path):
    preferences.save_profile(preferences.ScanProfile("Temporary"), tmp_path)
    assert preferences.delete_profile("temporary", tmp_path) is True
    assert preferences.delete_profile("temporary", tmp_path) is False
    with pytest.raises(ValueError, match="default"):
        preferences.delete_profile("DEFAULT", tmp_path)
    assert preferences.list_profiles(tmp_path) == [preferences.DEFAULT_PROFILE]


def test_selection_rules_round_trip_without_losing_profiles(tmp_path):
    preferences.save_profile(preferences.ScanProfile("Documents"), tmp_path)
    rules = preferences.SelectionRules(
        keep="oldest",
        always_keep_paths=("/Users/me/Documents",),
        prefer_keep_paths=("/Users/me/Archive",),
        prefer_remove_paths=("/Users/me/Downloads",),
        prefer_keep_internal=False,
    )
    preferences.save_selection_rules(rules, tmp_path)
    assert preferences.load_selection_rules(tmp_path) == rules
    assert preferences.load_profile("Documents", tmp_path).name == "Documents"


@pytest.mark.parametrize("keep", ["", "largest", 42])
def test_selection_rules_reject_unknown_policy(keep):
    with pytest.raises(ValueError):
        preferences.SelectionRules(keep=keep)


def test_choose_keeper_newest_and_oldest_are_input_order_independent():
    paths = ["/data/B.bin", "/data/a.bin", "/data/C.bin"]
    metadata = {
        "/data/a.bin": {"mtime_ns": 20},
        "/data/B.bin": {"mtime_ns": 10},
        "/data/C.bin": {"mtime_ns": 20},
    }
    newest = preferences.SelectionRules(keep="newest", prefer_keep_internal=False)
    oldest = preferences.SelectionRules(keep="oldest", prefer_keep_internal=False)
    assert preferences.choose_keeper(paths, metadata, newest) == "/data/a.bin"
    assert preferences.choose_keeper(list(reversed(paths)), metadata, newest) == "/data/a.bin"
    assert preferences.choose_keeper(paths, metadata, oldest) == "/data/B.bin"


def test_choose_keeper_rule_priorities():
    paths = [
        "/Users/me/Documents/copy.bin",
        "/Users/me/Downloads/copy.bin",
        "/Volumes/Backup/copy.bin",
    ]
    metadata = {path: {"mtime_ns": index + 1} for index, path in enumerate(paths)}

    always = preferences.SelectionRules(
        always_keep_paths=("/Volumes/Backup",),
        prefer_remove_paths=("/Volumes",),
    )
    assert preferences.choose_keeper(paths, metadata, always) == paths[2]

    keep_docs = preferences.SelectionRules(
        prefer_keep_paths=("/Users/me/Documents",), keep="newest"
    )
    assert preferences.choose_keeper(paths, metadata, keep_docs) == paths[0]

    avoid_downloads = preferences.SelectionRules(
        prefer_remove_paths=("/Users/me/Downloads",),
        keep="newest",
        prefer_keep_internal=False,
    )
    assert preferences.choose_keeper(paths[:2], metadata, avoid_downloads) == paths[0]

    internal = preferences.SelectionRules(keep="newest", prefer_keep_internal=True)
    assert preferences.choose_keeper(paths, metadata, internal) == paths[1]


@dataclass
class Metadata:
    mtime_ns: int
    is_internal: bool


def test_choose_keeper_accepts_object_metadata_and_uses_explicit_volume_kind():
    paths = ["/Volumes/A/a", "/Volumes/B/b"]
    metadata = {
        paths[0]: Metadata(mtime_ns=1, is_internal=True),
        paths[1]: Metadata(mtime_ns=2, is_internal=False),
    }
    assert preferences.choose_keeper(paths, metadata) == paths[0]


def test_choose_keeper_has_safe_empty_and_missing_metadata_fallbacks():
    assert preferences.choose_keeper([]) is None
    assert preferences.choose_keeper(["/b", "/A"], {}) == "/A"
    assert preferences.choose_keeper(["/same", "/same"]) == "/same"


def test_choose_removals_never_selects_every_copy_and_protects_all_keep_paths():
    paths = ["/keep/a", "/keep/b", "/other/c"]
    rules = preferences.SelectionRules(always_keep_paths=("/keep",))
    removals = preferences.choose_removals(paths, rules=rules)
    assert removals == ("/other/c",)
    assert set(removals) < set(paths)
    assert preferences.choose_removals(["/only"]) == ()


@pytest.mark.parametrize(
    "contents",
    [
        "{broken",
        json.dumps({"schema": "wrong", "version": 1, "profiles": []}),
        json.dumps(
            {
                "schema": "com.dupscan.preferences",
                "version": 1,
                "profiles": [preferences.DEFAULT_PROFILE.to_dict()] * 2,
                "selection_rules": preferences.DEFAULT_SELECTION_RULES.to_dict(),
            }
        ),
    ],
)
def test_corrupt_or_untrusted_store_is_rejected_without_overwrite(tmp_path, contents):
    path = tmp_path / "preferences.json"
    path.write_text(contents, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(preferences.PreferencesError):
        preferences.list_profiles(tmp_path)
    assert path.read_bytes() == before


def test_oversized_store_is_rejected_before_json_parsing(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_bytes(b" " * (preferences._MAX_STORE_BYTES + 1))
    with pytest.raises(preferences.PreferencesError, match="too large"):
        preferences.list_profiles(tmp_path)


def test_failed_atomic_replace_preserves_previous_store(tmp_path, monkeypatch):
    preferences.save_profile(preferences.ScanProfile("First"), tmp_path)
    path = tmp_path / "preferences.json"
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(preferences.os, "replace", fail_replace)
    with pytest.raises(preferences.PreferencesError, match="cannot save"):
        preferences.save_profile(preferences.ScanProfile("Second"), tmp_path)

    assert path.read_bytes() == before
    assert list(tmp_path.glob(".preferences-*.tmp")) == []

