"""Pure task/navigation state for the DupScan 2.0 desktop shell."""

from __future__ import annotations

from dataclasses import dataclass

TASK_SCAN = "scan"
TASK_RESULTS = "results"
TASK_COMPARE = "compare"
TASKS = (TASK_SCAN, TASK_RESULTS, TASK_COMPARE)


@dataclass(frozen=True)
class NavigationState:
    active_task: str
    results_enabled: bool
    scan_busy: bool


@dataclass(frozen=True)
class ResultSummary:
    files_seen: int
    file_groups: int
    directory_groups: int
    similarity_pairs: int
    reclaim_bytes: int
    read_only: bool


def navigation_state(
        requested_task: str,
        *,
        has_result: bool,
        scan_busy: bool,
) -> NavigationState:
    if requested_task not in TASKS:
        raise ValueError(f"unknown task: {requested_task}")
    active = TASK_SCAN if scan_busy else requested_task
    if active == TASK_RESULTS and not has_result:
        active = TASK_SCAN
    return NavigationState(active, has_result and not scan_busy, scan_busy)


def task_after_scan(*, cancelled: bool) -> str:
    return TASK_SCAN if cancelled else TASK_RESULTS


def selection_bar_visible(marked_count: int) -> bool:
    if marked_count < 0:
        raise ValueError("marked_count must be non-negative")
    return marked_count > 0


def summarize_result(result) -> ResultSummary:
    file_groups = tuple(getattr(result, "file_groups", ()))
    directory_groups = tuple(getattr(result, "dir_groups", ()))
    similarity_pairs = tuple(getattr(result, "sim_pairs", ()))
    reclaim = sum(max(0, int(getattr(group, "wasted", 0)))
                  for group in file_groups)
    return ResultSummary(
        files_seen=max(0, int(getattr(result, "files_seen", 0))),
        file_groups=len(file_groups),
        directory_groups=len(directory_groups),
        similarity_pairs=len(similarity_pairs),
        reclaim_bytes=reclaim,
        read_only=not bool(getattr(result, "live", False))
        or bool(getattr(result, "partial", False)),
    )
