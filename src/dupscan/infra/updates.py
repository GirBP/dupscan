"""Privacy-preserving, opt-in update manifest checks for DupScan.

This module deliberately does not download or install application updates.  It
only retrieves a small JSON manifest after the caller has obtained the user's
consent and returns a validated release page URL.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from functools import total_ordering
from pathlib import Path
from typing import Callable, Final
from urllib.parse import SplitResult, urlsplit


MANIFEST_SCHEMA_VERSION: Final = 1
DEFAULT_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_MAX_MANIFEST_BYTES: Final = 64 * 1024
HARD_MAX_MANIFEST_BYTES: Final = 1024 * 1024
MAX_URL_LENGTH: Final = 2048
MAX_VERSION_LENGTH: Final = 128
MAX_SUMMARY_LENGTH: Final = 8_000
BUNDLE_UPDATE_MANIFEST_KEY: Final = "DupScanUpdateManifestURL"
BUNDLE_SUPPORT_KEY: Final = "DupScanSupportURL"
_BUNDLE_ENDPOINT_KEYS: Final = frozenset({
    BUNDLE_UPDATE_MANIFEST_KEY,
    BUNDLE_SUPPORT_KEY,
})


class UpdateError(Exception):
    """Base class for update-check failures safe to display to a user."""


class UpdateValidationError(UpdateError):
    """A manifest, version, URL, or option did not pass strict validation."""


class UpdateFetchError(UpdateError):
    """The manifest could not be retrieved within the safety constraints."""


_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True, eq=False)
class SemanticVersion:
    """A strict Semantic Versioning 2.0.0 value."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> SemanticVersion:
        if not isinstance(value, str) or not value or len(value) > MAX_VERSION_LENGTH:
            raise UpdateValidationError("Invalid semantic version")
        match = _SEMVER_RE.fullmatch(value)
        if match is None:
            raise UpdateValidationError("Invalid semantic version")

        prerelease = tuple((match.group("prerelease") or "").split("."))
        if prerelease == ("",):
            prerelease = ()
        for identifier in prerelease:
            if identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0"):
                raise UpdateValidationError("Invalid semantic version prerelease")

        build = tuple((match.group("build") or "").split("."))
        if build == ("",):
            build = ()
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            prerelease,
            build,
        )

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        core_self = (self.major, self.minor, self.patch)
        core_other = (other.major, other.minor, other.patch)
        if core_self != core_other:
            return core_self < core_other
        return _prerelease_is_lower(self.prerelease, other.prerelease)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return False
        # Build metadata is intentionally ignored by SemVer precedence.
        return (
            self.major,
            self.minor,
            self.patch,
            self.prerelease,
        ) == (
            other.major,
            other.minor,
            other.patch,
            other.prerelease,
        )

    def __hash__(self) -> int:
        return hash((self.major, self.minor, self.patch, self.prerelease))


def _prerelease_is_lower(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left:
        return False
    if not right:
        return True
    for left_part, right_part in zip(left, right):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return int(left_part) < int(right_part)
        if left_numeric != right_numeric:
            return left_numeric
        return left_part < right_part
    return len(left) < len(right)


@dataclass(frozen=True)
class UpdateManifest:
    version: SemanticVersion
    release_url: str
    summary: str | None = None
    published_at: str | None = None


class UpdateStatus(str, Enum):
    DISABLED = "disabled"
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"


@dataclass(frozen=True)
class UpdateCheckResult:
    status: UpdateStatus
    current_version: SemanticVersion
    latest_version: SemanticVersion | None = None
    release_url: str | None = None
    summary: str | None = None
    published_at: str | None = None

    @property
    def update_available(self) -> bool:
        return self.status is UpdateStatus.UPDATE_AVAILABLE


# Injectable fetchers use positional arguments to remain easy to fake in tests.
ManifestFetcher = Callable[[str, float, int], bytes]


def check_for_update(
    manifest_url: str,
    current_version: str,
    *,
    enabled: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    fetcher: ManifestFetcher | None = None,
) -> UpdateCheckResult:
    """Check a release manifest only when ``enabled`` is explicitly true.

    No download or installation operation is provided.  A caller may open the
    returned HTTPS ``release_url`` after a separate user action.
    """

    current = SemanticVersion.parse(current_version)
    if not isinstance(enabled, bool):
        raise UpdateValidationError("Update consent flag must be a boolean")
    if not enabled:
        return UpdateCheckResult(UpdateStatus.DISABLED, current)

    validated_url = validate_https_url(manifest_url)
    validated_timeout = _validate_timeout(timeout)
    validated_max_bytes = _validate_max_bytes(max_bytes)
    fetch = fetcher or _fetch_https
    try:
        payload = fetch(validated_url, validated_timeout, validated_max_bytes)
    except UpdateError:
        raise
    except (OSError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise UpdateFetchError("Could not retrieve the update manifest") from exc
    except Exception as exc:
        # Do not leak an endpoint, proxy credentials, or a fetcher's internals.
        raise UpdateFetchError("Could not retrieve the update manifest") from exc

    if not isinstance(payload, bytes):
        raise UpdateFetchError("The update manifest response was not bytes")
    if len(payload) > validated_max_bytes:
        raise UpdateFetchError("The update manifest exceeded the size limit")

    manifest = parse_update_manifest(payload, max_bytes=validated_max_bytes)
    status = (
        UpdateStatus.UPDATE_AVAILABLE
        if manifest.version > current
        else UpdateStatus.UP_TO_DATE
    )
    return UpdateCheckResult(
        status=status,
        current_version=current,
        latest_version=manifest.version,
        release_url=manifest.release_url,
        summary=manifest.summary,
        published_at=manifest.published_at,
    )


def parse_update_manifest(
    payload: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
) -> UpdateManifest:
    """Decode and strictly validate a schema-v1 update manifest."""

    validated_max_bytes = _validate_max_bytes(max_bytes)
    if not isinstance(payload, bytes):
        raise UpdateValidationError("Manifest must be UTF-8 bytes")
    if not payload or len(payload) > validated_max_bytes:
        raise UpdateValidationError("Manifest has an invalid size")
    try:
        decoded = payload.decode("utf-8")
        document = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Invalid JSON constant: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise UpdateValidationError("Manifest is not valid UTF-8 JSON") from exc

    if not isinstance(document, dict):
        raise UpdateValidationError("Manifest root must be an object")
    required = {"schema_version", "version", "release_url"}
    optional = {"summary", "published_at"}
    keys = set(document)
    if not required.issubset(keys) or not keys.issubset(required | optional):
        raise UpdateValidationError("Manifest has missing or unknown fields")
    if type(document["schema_version"]) is not int:  # bool is not accepted as integer
        raise UpdateValidationError("Invalid manifest schema version")
    if document["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise UpdateValidationError("Unsupported manifest schema version")

    version = SemanticVersion.parse(document["version"])
    release_url = validate_https_url(document["release_url"])

    summary = document.get("summary")
    if summary is not None:
        if (
            not isinstance(summary, str)
            or len(summary) > MAX_SUMMARY_LENGTH
            or any(ord(character) == 0 for character in summary)
        ):
            raise UpdateValidationError("Invalid release summary")

    published_at = document.get("published_at")
    if published_at is not None and not _valid_timestamp(published_at):
        raise UpdateValidationError("Invalid publication timestamp")
    return UpdateManifest(version, release_url, summary, published_at)


def validate_https_url(value: object) -> str:
    """Return a canonical-enough HTTPS URL after strict structural checks.

    Параметр навмисно `object`, не `str` — перший рядок
    перевірки вже й так відхиляв нестрокові значення (наприклад,
    document.get(key) з plist може дати None); анотація тепер називає
    контракт, що й так виконувався.
    """

    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_LENGTH
        or not value.isascii()
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127
               for character in value)
    ):
        raise UpdateValidationError("Invalid HTTPS URL")
    try:
        parsed: SplitResult = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UpdateValidationError("Invalid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise UpdateValidationError("Invalid HTTPS URL")
    # A trailing dot and localhost are ambiguous in logs/proxy configuration and
    # are not suitable release endpoints.  Private hosts are still allowed for
    # managed enterprise deployments.
    hostname = parsed.hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".") or "." not in hostname:
        raise UpdateValidationError("Invalid HTTPS URL host")
    return value


def bundled_endpoint(key: str, *, executable: str | None = None) -> str | None:
    """Read one public HTTPS endpoint from the signed app Info.plist.

    Missing, malformed and unsupported values fail closed. No preference or
    environment fallback is allowed here because frozen release metadata must
    remain covered by the app signature.
    """

    if key not in _BUNDLE_ENDPOINT_KEYS:
        raise ValueError("Unsupported DupScan bundle endpoint key")
    plist_path = Path(executable or sys.executable).parent.parent / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            document = plistlib.load(handle)
        if not isinstance(document, dict):
            return None
        return validate_https_url(document.get(key))
    except (
        OSError,
        TypeError,
        ValueError,
        plistlib.InvalidFileException,
        UpdateValidationError,
    ):
        return None


def configured_endpoint(env_name: str, bundle_key: str) -> str | None:
    """Return a validated endpoint for frozen release or development.

    Frozen builds trust only signed Info.plist. Source/development runs may use
    an environment value so release configuration can be tested before build.
    """

    if bundle_key not in _BUNDLE_ENDPOINT_KEYS:
        raise ValueError("Unsupported DupScan bundle endpoint key")
    if getattr(sys, "frozen", False):
        return bundled_endpoint(bundle_key)
    value = os.environ.get(env_name, "")
    if not value:
        return None
    try:
        return validate_https_url(value)
    except UpdateValidationError:
        return None


def configured_manifest_url() -> str | None:
    return configured_endpoint(
        "DUPSCAN_UPDATE_MANIFEST_URL", BUNDLE_UPDATE_MANIFEST_KEY)


def configured_support_url() -> str | None:
    return configured_endpoint("DUPSCAN_SUPPORT_URL", BUNDLE_SUPPORT_KEY)


def _validate_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise UpdateValidationError("Invalid update timeout")
    timeout = float(value)
    if not 0 < timeout <= 30:
        raise UpdateValidationError("Update timeout must be between 0 and 30 seconds")
    return timeout


def _validate_max_bytes(value: int) -> int:
    if type(value) is not int or not 1 <= value <= HARD_MAX_MANIFEST_BYTES:
        raise UpdateValidationError("Invalid manifest size limit")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


_TIMESTAMP_RE = re.compile(
    r"^(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})Z$"
)


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str):
        return False
    match = _TIMESTAMP_RE.fullmatch(value)
    if match is None:
        return False
    try:
        from datetime import datetime, timezone

        datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=timezone.utc,
        )
    except ValueError:
        return False
    return True


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        validate_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_https(url: str, timeout: float, max_bytes: int) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "User-Agent": "DupScan-Update-Checker/1",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl()
            validate_https_url(final_url)
            status = response.getcode()
            if status != 200:
                raise UpdateFetchError("The update server returned an unexpected status")
            content_encoding = response.headers.get("Content-Encoding", "identity")
            if content_encoding.casefold() not in {"", "identity"}:
                raise UpdateFetchError("Compressed update manifests are not accepted")
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise UpdateFetchError("Invalid update manifest length") from exc
                if declared_length < 0 or declared_length > max_bytes:
                    raise UpdateFetchError("The update manifest exceeded the size limit")
            payload = response.read(max_bytes + 1)
    except UpdateError:
        raise
    except (OSError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise UpdateFetchError("Could not retrieve the update manifest") from exc
    if len(payload) > max_bytes:
        raise UpdateFetchError("The update manifest exceeded the size limit")
    return payload
