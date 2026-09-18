"""Pure state and batch-summary primitives for DupScan's scale UI.

This module deliberately has no Qt or filesystem dependency.  ``Main`` can
use these immutable values as the boundary between model/view state and a
later dry-run worker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Final, Literal, Mapping


Density = Literal["compact", "comfortable"]
SelectionScope = Literal["group", "filtered", "all_safe"]

VALID_DENSITIES: Final[frozenset[str]] = frozenset({"compact", "comfortable"})
VALID_SELECTION_SCOPES: Final[frozenset[str]] = frozenset(
    {"group", "filtered", "all_safe"}
)

# ``ready`` is the only state that may reach a destructive confirmation.  The
# remaining known values are retained so a dry-run can explain every skip.
READY_STATUS: Final[str] = "ready"
KNOWN_STATUSES: Final[frozenset[str]] = frozenset(
    {
        READY_STATUS,
        "pending",
        "conflict",
        "changed",
        "missing",
        "permission_denied",
        "offline",
        "network",
        "low_confidence",
        "cancelled",
        "failed",
    }
)
UNKNOWN_STATUS: Final[str] = "unknown"


def validate_density(value: object) -> Density:
    """Return a supported row density or raise without changing UI state."""
    if value not in VALID_DENSITIES:
        raise ValueError("density must be 'compact' or 'comfortable'")
    return value  # type: ignore[return-value]


def validate_selection_scope(value: object) -> SelectionScope:
    """Return a supported batch scope or raise without broadening a batch."""
    if value not in VALID_SELECTION_SCOPES:
        raise ValueError("selection scope must be group, filtered, or all_safe")
    return value  # type: ignore[return-value]


@dataclass(frozen=True)
class ViewPreset:
    """A serializable, immutable saved-view definition.

    Filters are descriptive only; a model adapter decides how to apply them.
    """

    key: str
    label: str
    statuses: tuple[str, ...] = ()
    source_kinds: tuple[str, ...] = ()
    sort_key: str = "default"
    descending: bool = False


PRESETS: Final[Mapping[str, ViewPreset]] = MappingProxyType(
    {
        "all": ViewPreset("all", "Усі"),
        "largest": ViewPreset(
            "largest", "Найбільша економія", sort_key="reclaimable_bytes", descending=True
        ),
        "safe": ViewPreset("safe", "Безпечні точні", statuses=(READY_STATUS,)),
        "review": ViewPreset(
            "review",
            "Потребують рішення",
            statuses=(
                "pending",
                "conflict",
                "changed",
                "missing",
                "permission_denied",
                "offline",
                "network",
                "low_confidence",
                UNKNOWN_STATUS,
            ),
        ),
        "network": ViewPreset("network", "Мережеві джерела", source_kinds=("network",)),
    }
)


@dataclass(frozen=True)
class ViewState:
    """Validated view choices suitable for persistence or a model adapter."""

    density: Density = "comfortable"
    selection_scope: SelectionScope = "group"
    preset: str = "all"

    def __post_init__(self) -> None:
        validate_density(self.density)
        validate_selection_scope(self.selection_scope)
        if self.preset not in PRESETS:
            raise ValueError(f"unknown view preset: {self.preset}")

    def with_density(self, density: object) -> ViewState:
        return replace(self, density=validate_density(density))

    def with_selection_scope(self, scope: object) -> ViewState:
        return replace(self, selection_scope=validate_selection_scope(scope))

    def with_preset(self, preset: str) -> ViewState:
        if preset not in PRESETS:
            raise ValueError(f"unknown view preset: {preset}")
        return replace(self, preset=preset)


@dataclass(frozen=True)
class BatchSummary:
    """Filesystem-free dry-run summary.

    ``count``/``bytes``/``groups`` describe all well-formed proposed entries;
    only ``ready_*`` entries are eligible for confirmation.  Unknown and
    malformed statuses are always represented in ``skipped_status_counts``.
    """

    count: int
    bytes: int
    groups: int
    ready_count: int
    ready_bytes: int
    ready_groups: int
    skipped_status_counts: Mapping[str, int]


def _entry_values(entry: object) -> tuple[int, object, str] | None:
    """Validate only the fields required for safe aggregation; no I/O."""
    if not isinstance(entry, dict):
        return None
    size = entry.get("size")
    group_id = entry.get("group_id")
    source_kind = entry.get("source_kind")
    if type(size) is not int or size < 0 or group_id is None or not isinstance(source_kind, str):
        return None
    try:
        hash(group_id)
    except TypeError:
        return None
    status = entry.get("status")
    return size, group_id, status if isinstance(status, str) else UNKNOWN_STATUS


def summarize_candidates(entries: object) -> BatchSummary:
    """Summarize candidate dictionaries without reading the filesystem.

    Invalid entries and every unknown status fail closed as ``unknown`` skips.
    ``entries`` may be any iterable; a non-iterable is treated as an empty
    proposal so callers never accidentally approve an unvalidated batch.
    """
    try:
        # Реальний код помилки тут call-overload, не
        # arg-type — попередній коментар нічого не гасив (mypy окремо
        # писав "not covered by"). entries: object — навмисно, щоб
        # неітерований вхід падав у except нижче, а не крашив виклик.
        iterator = iter(entries)  # type: ignore[call-overload]
    except TypeError:
        iterator = iter(())

    count = total_bytes = ready_count = ready_bytes = 0
    groups: set[object] = set()
    ready_groups: set[object] = set()
    skipped: dict[str, int] = {}
    for entry in iterator:
        values = _entry_values(entry)
        if values is None:
            skipped[UNKNOWN_STATUS] = skipped.get(UNKNOWN_STATUS, 0) + 1
            continue
        size, group_id, status = values
        count += 1
        total_bytes += size
        groups.add(group_id)
        if status == READY_STATUS:
            ready_count += 1
            ready_bytes += size
            ready_groups.add(group_id)
        else:
            safe_status = status if status in KNOWN_STATUSES else UNKNOWN_STATUS
            skipped[safe_status] = skipped.get(safe_status, 0) + 1

    return BatchSummary(
        count=count,
        bytes=total_bytes,
        groups=len(groups),
        ready_count=ready_count,
        ready_bytes=ready_bytes,
        ready_groups=len(ready_groups),
        skipped_status_counts=MappingProxyType(dict(sorted(skipped.items()))),
    )
