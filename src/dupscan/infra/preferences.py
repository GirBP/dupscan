"""Persistent scan profiles and deterministic automatic-selection rules.

This module deliberately has no Qt or DupScan-core dependency.  It can be
used by the scanner, preferences UI and tests without touching the file
system except through the explicit persistence functions near the bottom.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

_SCHEMA = "com.dupscan.preferences"
_VERSION = 1
_MAX_STORE_BYTES = 2 * 1024 * 1024
_MAX_PROFILES = 100
_MAX_LIST_ITEMS = 10_000
_MAX_PATH_LENGTH = 32_768
_PROFILE_FIELDS = {
    "name",
    "min_size",
    "include_extensions",
    "excluded_extensions",
    "excluded_paths",
    "include_hidden",
    "include_bundles",
    "include_symlinks",
}
_RULE_FIELDS = {
    "keep",
    "always_keep_paths",
    "prefer_keep_paths",
    "prefer_remove_paths",
    "prefer_keep_internal",
}
_KEEP_POLICIES = frozenset({"newest", "oldest", "lexical"})


class PreferencesError(ValueError):
    """The preferences store is unreadable, unsafe or has an unknown schema."""


def _require_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _normalize_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("profile name must be a string")
    name = unicodedata.normalize("NFC", value.strip())
    if not name or len(name) > 80:
        raise ValueError("profile name must contain 1 to 80 characters")
    if any(ord(char) < 32 or char in "\x7f/\\" for char in name):
        raise ValueError("profile name contains an unsafe character")
    return name


def _normalize_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("min_size must be an integer")
    if value < 0 or value > (1 << 63) - 1:
        raise ValueError("min_size is outside the supported range")
    return value


def _normalize_extension(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("extensions must be strings")
    extension = unicodedata.normalize("NFC", value.strip()).casefold()
    if extension and not extension.startswith("."):
        extension = "." + extension
    if (
        len(extension) < 2
        or len(extension) > 32
        or any(char in extension for char in ("/", "\\", "\0"))
        or any(char.isspace() for char in extension)
    ):
        raise ValueError(f"unsafe extension: {value!r}")
    return extension


def _normalize_items(values: object, normalizer, label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError(f"{label} must be a sequence")
    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if index >= _MAX_LIST_ITEMS:
            raise ValueError(f"{label} contains too many items")
        item = normalizer(value)
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(item)
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def _normalize_rule_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("preference paths must be non-empty strings")
    expanded = os.path.expanduser(unicodedata.normalize("NFC", value.strip()))
    if not expanded or len(expanded) > _MAX_PATH_LENGTH or not os.path.isabs(expanded):
        raise ValueError(f"preference path must be absolute: {value!r}")
    return os.path.normpath(expanded)


def _normalize_paths(values: object, label: str) -> tuple[str, ...]:
    return _normalize_items(values, _normalize_rule_path, label)


@dataclass(frozen=True, slots=True)
class ScanProfile:
    """Validated scanner filter settings.

    An empty ``include_extensions`` tuple means all extensions.  Exclusions
    always win.  The built-in defaults preserve DupScan's current behaviour:
    hidden files and package contents are included, while symlinks are not
    followed as regular files.
    """

    name: str
    min_size: int = 0
    include_extensions: tuple[str, ...] = field(default_factory=tuple)
    excluded_extensions: tuple[str, ...] = field(default_factory=tuple)
    excluded_paths: tuple[str, ...] = field(default_factory=tuple)
    include_hidden: bool = True
    include_bundles: bool = True
    include_symlinks: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalize_name(self.name))
        object.__setattr__(self, "min_size", _normalize_size(self.min_size))
        includes = _normalize_items(
            self.include_extensions, _normalize_extension, "include_extensions"
        )
        excludes = _normalize_items(
            self.excluded_extensions, _normalize_extension, "excluded_extensions"
        )
        overlap = set(includes).intersection(excludes)
        if overlap:
            raise ValueError(
                "extensions cannot be both included and excluded: "
                + ", ".join(sorted(overlap))
            )
        object.__setattr__(self, "include_extensions", includes)
        object.__setattr__(self, "excluded_extensions", excludes)
        object.__setattr__(
            self, "excluded_paths", _normalize_paths(self.excluded_paths, "excluded_paths")
        )
        object.__setattr__(
            self, "include_hidden", _require_bool(self.include_hidden, "include_hidden")
        )
        object.__setattr__(
            self, "include_bundles", _require_bool(self.include_bundles, "include_bundles")
        )
        object.__setattr__(
            self,
            "include_symlinks",
            _require_bool(self.include_symlinks, "include_symlinks"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "min_size": self.min_size,
            "include_extensions": list(self.include_extensions),
            "excluded_extensions": list(self.excluded_extensions),
            "excluded_paths": list(self.excluded_paths),
            "include_hidden": self.include_hidden,
            "include_bundles": self.include_bundles,
            "include_symlinks": self.include_symlinks,
        }

    @classmethod
    def from_dict(cls, data: object) -> ScanProfile:
        if not isinstance(data, dict) or set(data) != _PROFILE_FIELDS:
            raise ValueError("scan profile has missing or unknown fields")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class SelectionRules:
    """Priority rules used to choose the one duplicate that must survive.

    Priority is: always-keep path, preferred-keep path, avoid preferred-remove
    paths, prefer an internal volume, then ``keep``.  Path rules match both an
    exact path and descendants of a directory path.
    """

    keep: str = "newest"
    always_keep_paths: tuple[str, ...] = field(default_factory=tuple)
    prefer_keep_paths: tuple[str, ...] = field(default_factory=tuple)
    prefer_remove_paths: tuple[str, ...] = field(default_factory=tuple)
    prefer_keep_internal: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.keep, str) or self.keep not in _KEEP_POLICIES:
            raise ValueError(f"keep must be one of: {', '.join(sorted(_KEEP_POLICIES))}")
        object.__setattr__(
            self,
            "always_keep_paths",
            _normalize_paths(self.always_keep_paths, "always_keep_paths"),
        )
        object.__setattr__(
            self,
            "prefer_keep_paths",
            _normalize_paths(self.prefer_keep_paths, "prefer_keep_paths"),
        )
        object.__setattr__(
            self,
            "prefer_remove_paths",
            _normalize_paths(self.prefer_remove_paths, "prefer_remove_paths"),
        )
        object.__setattr__(
            self,
            "prefer_keep_internal",
            _require_bool(self.prefer_keep_internal, "prefer_keep_internal"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "keep": self.keep,
            "always_keep_paths": list(self.always_keep_paths),
            "prefer_keep_paths": list(self.prefer_keep_paths),
            "prefer_remove_paths": list(self.prefer_remove_paths),
            "prefer_keep_internal": self.prefer_keep_internal,
        }

    @classmethod
    def from_dict(cls, data: object) -> SelectionRules:
        if not isinstance(data, dict) or set(data) != _RULE_FIELDS:
            raise ValueError("selection rules have missing or unknown fields")
        return cls(**data)


DEFAULT_PROFILE_NAME = "Default"
DEFAULT_PROFILE = ScanProfile(name=DEFAULT_PROFILE_NAME)
DEFAULT_SELECTION_RULES = SelectionRules()


def _path_is_within(path: str, parent: str) -> bool:
    if not os.path.isabs(path):
        return False
    try:
        return os.path.commonpath((os.path.normpath(path), parent)) == parent
    except ValueError:
        return False


def _matches_any_path(path: str, parents: Sequence[str]) -> bool:
    return any(_path_is_within(path, parent) for parent in parents)


def should_include(
    path: str,
    name: str | None = None,
    size: int = 0,
    is_hidden: bool = False,
    is_bundle: bool = False,
    profile: ScanProfile = DEFAULT_PROFILE,
    is_symlink: bool = False,
) -> bool:
    """Return whether one scanner entry passes ``profile`` without doing I/O."""

    if not isinstance(profile, ScanProfile):
        raise TypeError("profile must be a ScanProfile")
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("path must be a non-empty string")
    if name is None:
        entry_name = os.path.basename(os.path.normpath(path))
    elif isinstance(name, str) and name and "\0" not in name:
        entry_name = name
    else:
        raise ValueError("name must be a non-empty string or None")
    entry_size = _normalize_size(size)
    hidden = _require_bool(is_hidden, "is_hidden") or entry_name.startswith(".")
    bundle = _require_bool(is_bundle, "is_bundle")
    symlink = _require_bool(is_symlink, "is_symlink")

    if entry_size < profile.min_size:
        return False
    if hidden and not profile.include_hidden:
        return False
    if bundle and not profile.include_bundles:
        return False
    if symlink and not profile.include_symlinks:
        return False
    if _matches_any_path(os.path.normpath(path), profile.excluded_paths):
        return False

    folded_name = unicodedata.normalize("NFC", entry_name).casefold()
    if any(folded_name.endswith(extension) for extension in profile.excluded_extensions):
        return False
    if profile.include_extensions and not any(
        folded_name.endswith(extension) for extension in profile.include_extensions
    ):
        return False
    return True


def _candidate_paths(paths: Sequence[str]) -> list[str]:
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Sequence):
        raise TypeError("paths must be a sequence of strings")
    by_identity: dict[str, str] = {}
    for path in paths:
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("candidate paths must be non-empty strings")
        if len(path) > _MAX_PATH_LENGTH:
            raise ValueError("candidate path is too long")
        identity = os.path.normpath(path)
        previous = by_identity.get(identity)
        if previous is None or _path_sort_key(path) < _path_sort_key(previous):
            by_identity[identity] = path
    return sorted(by_identity.values(), key=_path_sort_key)


def _path_sort_key(path: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFC", os.path.normpath(path))
    return normalized.casefold(), normalized


def _metadata_item(metadata: Mapping[str, object] | None, path: str) -> object | None:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping or None")
    return metadata.get(path)


def _field_value(item: object | None, names: Sequence[str]) -> object | None:
    if item is None:
        return None
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _timestamp(metadata: Mapping[str, object] | None, path: str) -> float | None:
    item = _metadata_item(metadata, path)
    value = _field_value(item, ("mtime_ns", "modified_ns", "mtime", "btime_ns"))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _is_internal(metadata: Mapping[str, object] | None, path: str) -> bool:
    item = _metadata_item(metadata, path)
    explicit = _field_value(item, ("is_internal",))
    if isinstance(explicit, bool):
        return explicit
    normalized = os.path.normpath(path)
    return not (normalized == "/Volumes" or normalized.startswith("/Volumes/"))


def choose_keeper(
    paths: Sequence[str],
    metadata: Mapping[str, object] | None = None,
    rules: SelectionRules = DEFAULT_SELECTION_RULES,
) -> str | None:
    """Choose exactly one deterministic survivor, or ``None`` for no paths.

    The function never stats files and never relies on input order.  Therefore
    a caller can safely select all *other* unique paths without accidentally
    selecting every copy.
    """

    if not isinstance(rules, SelectionRules):
        raise TypeError("rules must be SelectionRules")
    candidates = _candidate_paths(paths)
    if not candidates:
        return None

    protected = [
        path for path in candidates if _matches_any_path(path, rules.always_keep_paths)
    ]
    if protected:
        candidates = protected
    else:
        preferred = [
            path for path in candidates if _matches_any_path(path, rules.prefer_keep_paths)
        ]
        if preferred:
            candidates = preferred

        ordinary = [
            path
            for path in candidates
            if not _matches_any_path(path, rules.prefer_remove_paths)
        ]
        if ordinary:
            candidates = ordinary

        if rules.prefer_keep_internal:
            internal = [path for path in candidates if _is_internal(metadata, path)]
            if internal:
                candidates = internal

    if rules.keep == "lexical":
        return min(candidates, key=_path_sort_key)

    dated = [(path, _timestamp(metadata, path)) for path in candidates]
    known = [(path, timestamp) for path, timestamp in dated if timestamp is not None]
    if not known:
        return min(candidates, key=_path_sort_key)
    if rules.keep == "newest":
        return min(known, key=lambda item: (-item[1], _path_sort_key(item[0])))[0]
    return min(known, key=lambda item: (item[1], _path_sort_key(item[0])))[0]


def choose_removals(
    paths: Sequence[str],
    metadata: Mapping[str, object] | None = None,
    rules: SelectionRules = DEFAULT_SELECTION_RULES,
) -> tuple[str, ...]:
    """Return deterministic removal candidates while protecting all keep paths."""

    candidates = _candidate_paths(paths)
    keeper = choose_keeper(candidates, metadata, rules)
    if keeper is None:
        return ()
    removals = [
        path
        for path in candidates
        if path != keeper and not _matches_any_path(path, rules.always_keep_paths)
    ]
    # Defensive invariant: even future rule changes must leave a survivor.
    if len(removals) >= len(candidates):
        removals.remove(keeper)
    return tuple(removals)


def default_data_dir() -> str:
    """Return DupScan's data directory, honouring the test/deployment override."""

    override = os.environ.get("DUPSCAN_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.expanduser("~/Library/Application Support/DupScan")


def preferences_path(base_dir: str | os.PathLike[str] | None = None) -> str:
    base = default_data_dir() if base_dir is None else os.fspath(base_dir)
    if not base or "\0" in base:
        raise ValueError("base_dir must be a valid path")
    return os.path.join(os.path.abspath(os.path.expanduser(base)), "preferences.json")


def verification_path(base_dir: str | os.PathLike[str] | None = None) -> str:
    """Окремий файл перемикача перевірки.

    Схема preferences.json строга і відхиляє невідомі секції, тому режим
    перевірки живе окремо — щоб не ламати формат і міграції профілів.
    """
    base = default_data_dir() if base_dir is None else os.fspath(base_dir)
    return os.path.join(os.path.abspath(os.path.expanduser(base)),
                        "verification.json")


def paranoid_verification(base_dir: str | os.PathLike[str] | None = None) -> bool:
    """Чи вимагати повне перечитування навіть за чинного повного доказу.

    За замовчуванням False: група дублікатів не може виникнути без повного
    BLAKE3, тому незмінені метадані вже доводять вміст. Вмикається змінною
    середовища DUPSCAN_PARANOID=1 або файлом verification.json.
    """
    env = os.environ.get("DUPSCAN_PARANOID", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    try:
        with open(verification_path(base_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("paranoid") is True)


def set_paranoid_verification(
    value: bool, base_dir: str | os.PathLike[str] | None = None
) -> None:
    """Записати перемикач; best-effort, помилка не валить застосунок."""
    path = verification_path(base_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"paranoid": bool(value)}, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def _default_store() -> tuple[list[ScanProfile], SelectionRules]:
    return [DEFAULT_PROFILE], DEFAULT_SELECTION_RULES


def _decode_store(data: object) -> tuple[list[ScanProfile], SelectionRules]:
    if not isinstance(data, dict):
        raise PreferencesError("preferences root must be an object")
    if set(data) != {"schema", "version", "profiles", "selection_rules"}:
        raise PreferencesError("preferences have missing or unknown sections")
    if data.get("schema") != _SCHEMA or data.get("version") != _VERSION:
        raise PreferencesError("unsupported preferences format")
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, list) or len(raw_profiles) > _MAX_PROFILES:
        raise PreferencesError("invalid number of scan profiles")
    try:
        profiles = [ScanProfile.from_dict(item) for item in raw_profiles]
        rules = SelectionRules.from_dict(data.get("selection_rules"))
    except (TypeError, ValueError) as error:
        raise PreferencesError(f"invalid preferences: {error}") from error

    keys = [profile.name.casefold() for profile in profiles]
    if len(keys) != len(set(keys)):
        raise PreferencesError("duplicate profile names")
    if DEFAULT_PROFILE_NAME.casefold() not in keys:
        profiles.append(DEFAULT_PROFILE)
    return profiles, rules


def _read_store(
    base_dir: str | os.PathLike[str] | None,
) -> tuple[list[ScanProfile], SelectionRules]:
    path = preferences_path(base_dir)
    try:
        size = os.path.getsize(path)
    except FileNotFoundError:
        return _default_store()
    except OSError as error:
        raise PreferencesError(f"cannot inspect preferences: {error}") from error
    if size > _MAX_STORE_BYTES:
        raise PreferencesError("preferences file is too large")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreferencesError(f"cannot read preferences: {error}") from error
    return _decode_store(data)


def _encode_store(
    profiles: Sequence[ScanProfile], rules: SelectionRules
) -> dict[str, object]:
    return {
        "schema": _SCHEMA,
        "version": _VERSION,
        "profiles": [profile.to_dict() for profile in profiles],
        "selection_rules": rules.to_dict(),
    }


def _write_store(
    profiles: Sequence[ScanProfile],
    rules: SelectionRules,
    base_dir: str | os.PathLike[str] | None,
) -> None:
    path = preferences_path(base_dir)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".preferences-", suffix=".tmp", dir=directory
        )
    except OSError as error:
        raise PreferencesError(f"cannot prepare preferences: {error}") from error

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                _encode_store(profiles, rules),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The file itself is already durable; some filesystems reject a
            # directory fsync, so this optional durability step is best-effort.
            pass
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise PreferencesError(f"cannot save preferences: {error}") from error


def list_profiles(
    base_dir: str | os.PathLike[str] | None = None,
) -> list[ScanProfile]:
    profiles, _ = _read_store(base_dir)
    return sorted(
        profiles,
        key=lambda profile: (
            profile.name.casefold() != DEFAULT_PROFILE_NAME.casefold(),
            profile.name.casefold(),
            profile.name,
        ),
    )


def load_profile(
    name: str = DEFAULT_PROFILE_NAME,
    base_dir: str | os.PathLike[str] | None = None,
) -> ScanProfile:
    wanted = _normalize_name(name).casefold()
    for profile in list_profiles(base_dir):
        if profile.name.casefold() == wanted:
            return profile
    raise KeyError(name)


def save_profile(
    profile: ScanProfile,
    base_dir: str | os.PathLike[str] | None = None,
) -> ScanProfile:
    if not isinstance(profile, ScanProfile):
        raise TypeError("profile must be a ScanProfile")
    profiles, rules = _read_store(base_dir)
    key = profile.name.casefold()
    replaced = False
    updated: list[ScanProfile] = []
    for current in profiles:
        if current.name.casefold() == key:
            if not replaced:
                updated.append(profile)
                replaced = True
        else:
            updated.append(current)
    if not replaced:
        if len(updated) >= _MAX_PROFILES:
            raise PreferencesError("too many scan profiles")
        updated.append(profile)
    _write_store(updated, rules, base_dir)
    return profile


def delete_profile(
    name: str,
    base_dir: str | os.PathLike[str] | None = None,
) -> bool:
    key = _normalize_name(name).casefold()
    if key == DEFAULT_PROFILE_NAME.casefold():
        raise ValueError("the default profile cannot be deleted")
    profiles, rules = _read_store(base_dir)
    updated = [profile for profile in profiles if profile.name.casefold() != key]
    if len(updated) == len(profiles):
        return False
    _write_store(updated, rules, base_dir)
    return True


def load_selection_rules(
    base_dir: str | os.PathLike[str] | None = None,
) -> SelectionRules:
    _, rules = _read_store(base_dir)
    return rules


def save_selection_rules(
    rules: SelectionRules,
    base_dir: str | os.PathLike[str] | None = None,
) -> SelectionRules:
    if not isinstance(rules, SelectionRules):
        raise TypeError("rules must be SelectionRules")
    profiles, _ = _read_store(base_dir)
    _write_store(profiles, rules, base_dir)
    return rules


# --- теки-еталони (reference roots, 2.23.0) --------------------------------
#
# Концепт dupeGuru: позначена тека недоторканна за визначенням. Це НЕ
# always_keep_paths (той лише зсуває пріоритет автовибору) — еталон
# твердо блокує деструктив на шести шарах (див. tests/test_reference_roots).
# Окремий файл, не поле store: захисний список не мусить ділити долю
# (і ліміти, і міграції) з профілями скану.


def reference_roots_path(base_dir: str | os.PathLike[str] | None = None) -> str:
    base = default_data_dir() if base_dir is None else os.fspath(base_dir)
    if not base or "\0" in base:
        raise ValueError("base_dir must be a valid path")
    return os.path.join(
        os.path.abspath(os.path.expanduser(base)), "reference_roots.json")


def load_reference_roots(
    base_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Список тек-еталонів (realpath). Немає файла → порожньо; зіпсований
    файл → PreferencesError, і викликач МУСИТЬ трактувати це fail-closed
    (захист не можна тихо втратити через биту конфігурацію)."""
    path = reference_roots_path(base_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreferencesError(
            f"cannot read reference roots: {error}") from error
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("roots"), list)
        or any(not isinstance(item, str) or not item for item in data["roots"])
    ):
        raise PreferencesError("reference roots file is malformed")
    return tuple(dict.fromkeys(
        os.path.realpath(item) for item in data["roots"]))


def save_reference_roots(
    paths: Iterable[str],
    base_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Зберегти еталони. Кожен шлях зводиться до realpath НА ЗАПИСІ —
    symlink-псевдонім ніколи не потрапляє у сховище."""
    roots = tuple(dict.fromkeys(
        os.path.realpath(os.fspath(path)) for path in paths))
    if len(roots) > _MAX_LIST_ITEMS:
        raise PreferencesError("too many reference roots")
    path = reference_roots_path(base_dir)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".reference-", suffix=".tmp", dir=directory)
    except OSError as error:
        raise PreferencesError(
            f"cannot prepare reference roots: {error}") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema": _SCHEMA, "version": _VERSION,
                       "roots": list(roots)}, handle, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise PreferencesError(
            f"cannot write reference roots: {error}") from error
    return roots


def is_protected(path: str, roots: Sequence[str]) -> bool:
    """Чи лежить path усередині (або Є) теки-еталона.

    realpath ОБОХ боків: обхід через symlink на еталон не проходить
    (roots уже realpath зі save/load, path розв'язується тут)."""
    if not roots:
        return False
    real = os.path.realpath(path)
    return any(_path_is_within(real, root) for root in roots)


def select_victims(
    candidates: Sequence[str],
    rules: SelectionRules = DEFAULT_SELECTION_RULES,
    *,
    metadata: Mapping[str, object] | None = None,
    reference_roots: Sequence[str] = (),
) -> list[str]:
    """Жертви автовибору: всі, крім хранителя І крім еталонних.

    Еталон фільтрується З ЖЕРТВ, а не додається до always_keep: навіть
    якби правила чомусь обрали хранителя поза еталоном, еталонні шляхи
    все одно не можуть стати жертвами."""
    keeper = choose_keeper(candidates, metadata, rules)
    return [
        path for path in candidates
        if path != keeper and not is_protected(path, reference_roots)
    ]


def is_protected_lexical(path: str, roots: Sequence[str]) -> bool:
    """Лексична (БЕЗ диска) перевірка еталона — для GUI-потоку.

    realpath ходить по lstat кожного компонента, а інваріант продукту:
    GUI-потік не торкається диска (перевірено тестом, який сповільнює
    lstat і міряє блокування — саме він і зловив realpath у гарді).
    Ця перевірка ловить прямі шляхи; обхід через symlink добиває
    справжній is_protected на останньому рубежі to_trash, що завжди
    виконується у фоновому потоці."""
    if not roots:
        return False
    normalized = os.path.normpath(os.path.abspath(path))
    return any(_path_is_within(normalized, root) for root in roots)
