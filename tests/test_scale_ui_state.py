"""Pure scale-UI state and dry-run summary contract."""

from dataclasses import FrozenInstanceError

import pytest

import dupscan.ui.scale_ui as scale_ui


@pytest.mark.parametrize("value", ["compact", "comfortable"])
def test_density_is_strictly_validated(value):
    assert scale_ui.validate_density(value) == value


@pytest.mark.parametrize("value", [None, "spacious", 1, True])
def test_invalid_density_is_rejected(value):
    with pytest.raises(ValueError, match="density"):
        scale_ui.validate_density(value)


@pytest.mark.parametrize("value", ["group", "filtered", "all_safe"])
def test_selection_scope_is_strictly_validated(value):
    assert scale_ui.validate_selection_scope(value) == value


@pytest.mark.parametrize("value", [None, "visible", "all", 0])
def test_invalid_selection_scope_is_rejected(value):
    with pytest.raises(ValueError, match="selection scope"):
        scale_ui.validate_selection_scope(value)


def test_view_state_and_presets_are_immutable_and_validated():
    state = scale_ui.ViewState()
    assert state.with_density("compact").density == "compact"
    assert state.with_selection_scope("all_safe").selection_scope == "all_safe"
    assert state.with_preset("network").preset == "network"
    with pytest.raises(FrozenInstanceError):
        state.density = "compact"  # type: ignore[misc]
    with pytest.raises(TypeError):
        scale_ui.PRESETS["new"] = scale_ui.PRESETS["all"]  # type: ignore[index]
    with pytest.raises(ValueError, match="preset"):
        scale_ui.ViewState(preset="invented")


def test_presets_cover_product_views_without_mutable_filter_lists():
    assert set(scale_ui.PRESETS) == {"all", "largest", "safe", "review", "network"}
    assert scale_ui.PRESETS["largest"].descending
    assert scale_ui.PRESETS["safe"].statuses == ("ready",)
    assert scale_ui.PRESETS["network"].source_kinds == ("network",)


def test_summarize_candidates_counts_ready_groups_bytes_and_skips():
    summary = scale_ui.summarize_candidates(
        [
            {"size": 10, "group_id": "a", "status": "ready", "source_kind": "local"},
            {"size": 20, "group_id": "a", "status": "offline", "source_kind": "network"},
            {"size": 30, "group_id": "b", "status": "ready", "source_kind": "local"},
            {"size": 40, "group_id": "c", "status": "changed", "source_kind": "local"},
        ]
    )
    assert summary.count == 4
    assert summary.bytes == 100
    assert summary.groups == 3
    assert summary.ready_count == 2
    assert summary.ready_bytes == 40
    assert summary.ready_groups == 2
    assert dict(summary.skipped_status_counts) == {"changed": 1, "offline": 1}


def test_unknown_or_malformed_entries_fail_closed_without_filesystem_io(monkeypatch):
    monkeypatch.setattr(
        "os.stat", lambda *_args, **_kwargs: pytest.fail("summary must not stat")
    )
    summary = scale_ui.summarize_candidates(
        [
            {"size": 1, "group_id": "a", "status": "invented", "source_kind": "local"},
            {"size": -1, "group_id": "b", "status": "ready", "source_kind": "local"},
            {"size": 3, "group_id": [], "status": "ready", "source_kind": "local"},
            {"size": 4, "group_id": "d", "status": "ready"},
            object(),
        ]
    )
    assert summary.count == 1
    assert summary.ready_count == 0
    assert summary.bytes == 1
    assert dict(summary.skipped_status_counts) == {"unknown": 5}
    with pytest.raises(TypeError):
        summary.skipped_status_counts["ready"] = 1  # type: ignore[index]
