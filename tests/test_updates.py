"""Security and behavior contract for opt-in update checks."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.updates as updates
from dupscan.infra.updates import (
    SemanticVersion,
    UpdateFetchError,
    UpdateStatus,
    UpdateValidationError,
    check_for_update,
    parse_update_manifest,
    validate_https_url,
)


MANIFEST_URL = "https://updates.dupscan.example/manifest-v1.json"
RELEASE_URL = "https://dupscan.example/releases/1.2.0"


def manifest_bytes(**overrides):
    document = {
        "schema_version": 1,
        "version": "1.2.0",
        "release_url": RELEASE_URL,
        "summary": "Faster review and safer cleanup.",
        "published_at": "2026-07-22T10:30:00Z",
    }
    document.update(overrides)
    return json.dumps(document).encode()


def test_disabled_check_never_touches_network_or_even_validates_endpoint(monkeypatch):
    def network_must_not_run(*_args, **_kwargs):
        raise AssertionError("network was used without consent")

    monkeypatch.setattr(updates.urllib.request, "build_opener", network_must_not_run)
    result = check_for_update("http://not-allowed.invalid", "1.0.0")
    assert result.status is UpdateStatus.DISABLED
    assert result.latest_version is None
    assert not result.update_available


def test_injected_fetcher_receives_limits_and_reports_newer_release():
    calls = []

    def fetcher(url, timeout, max_bytes):
        calls.append((url, timeout, max_bytes))
        return manifest_bytes()

    result = check_for_update(
        MANIFEST_URL,
        "1.1.9",
        enabled=True,
        timeout=2.5,
        max_bytes=4096,
        fetcher=fetcher,
    )
    assert calls == [(MANIFEST_URL, 2.5, 4096)]
    assert result.status is UpdateStatus.UPDATE_AVAILABLE
    assert result.update_available
    assert str(result.latest_version) == "1.2.0"
    assert result.release_url == RELEASE_URL
    assert result.summary == "Faster review and safer cleanup."


@pytest.mark.parametrize("latest", ["1.2.0", "1.1.9", "1.2.0+different-build"])
def test_equal_or_older_versions_are_up_to_date(latest):
    result = check_for_update(
        MANIFEST_URL,
        "1.2.0",
        enabled=True,
        fetcher=lambda *_args: manifest_bytes(version=latest),
    )
    assert result.status is UpdateStatus.UP_TO_DATE


def test_semver_precedence_follows_semver_2_rules():
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    parsed = [SemanticVersion.parse(value) for value in ordered]
    assert parsed == sorted(reversed(parsed))
    assert SemanticVersion.parse("1.0.0+one") == SemanticVersion.parse("1.0.0+two")
    assert hash(SemanticVersion.parse("1.0.0+one")) == hash(
        SemanticVersion.parse("1.0.0+two")
    )


@pytest.mark.parametrize(
    "value",
    [
        "1",
        "1.0",
        "01.0.0",
        "1.01.0",
        "1.0.01",
        "v1.0.0",
        "1.0.0-01",
        "1.0.0-",
        "1.0.0+",
        "1.0.0_foo",
        "1.0.0-β",
        "1.0.0\n",
    ],
)
def test_invalid_semantic_versions_are_rejected(value):
    with pytest.raises(UpdateValidationError):
        SemanticVersion.parse(value)


@pytest.mark.parametrize(
    "url",
    [
        "http://dupscan.example/manifest.json",
        "file:///tmp/manifest.json",
        "https://localhost/manifest.json",
        "https://intranet/manifest.json",
        "https://user:password@dupscan.example/manifest.json",
        "https://dupscan.example/manifest.json#fragment",
        "https://dupscan.example./manifest.json",
        "https://dupscan.example:99999/manifest.json",
        "https://dupscan.example/manifest json",
        "https://дубскан.example/manifest.json",
    ],
)
def test_unsafe_or_ambiguous_urls_are_rejected(url):
    with pytest.raises(UpdateValidationError):
        validate_https_url(url)


def test_valid_enterprise_https_endpoint_is_allowed():
    assert (
        validate_https_url("https://10.0.0.5:8443/updates?channel=stable")
        == "https://10.0.0.5:8443/updates?channel=stable"
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{}",
        b'{"schema_version":true,"version":"1.2.0",'
        b'"release_url":"https://dupscan.example/release"}',
        b'{"schema_version":1,"schema_version":1,"version":"1.2.0",'
        b'"release_url":"https://dupscan.example/release"}',
        manifest_bytes(extra="unknown"),
        manifest_bytes(schema_version=2),
        manifest_bytes(release_url="javascript:alert(1)"),
        manifest_bytes(published_at="2026-02-30T00:00:00Z"),
        manifest_bytes(published_at="2026-07-22T10:30:00+00:00"),
        manifest_bytes(summary="x\x00y"),
        b'{"schema_version":1,"version":"1.2.0","release_url":NaN}',
        b"\xff",
    ],
)
def test_manifest_schema_is_strict(payload):
    with pytest.raises(UpdateValidationError):
        parse_update_manifest(payload)


def test_manifest_and_fetcher_size_caps_are_enforced():
    oversized = b"x" * 101
    with pytest.raises(UpdateValidationError):
        parse_update_manifest(oversized, max_bytes=100)
    with pytest.raises(UpdateFetchError):
        check_for_update(
            MANIFEST_URL,
            "1.0.0",
            enabled=True,
            max_bytes=100,
            fetcher=lambda *_args: oversized,
        )


@pytest.mark.parametrize("timeout", [0, -1, 31, True, "5"])
def test_timeout_is_strictly_bounded_before_fetch(timeout):
    called = False

    def fetcher(*_args):
        nonlocal called
        called = True
        return manifest_bytes()

    with pytest.raises(UpdateValidationError):
        check_for_update(
            MANIFEST_URL, "1.0.0", enabled=True, timeout=timeout, fetcher=fetcher
        )
    assert not called


def test_fetch_failures_do_not_leak_endpoint_credentials_or_internal_details():
    def failing_fetcher(*_args):
        raise RuntimeError("token=super-secret at /Users/alice/private")

    with pytest.raises(UpdateFetchError) as raised:
        check_for_update(
            MANIFEST_URL,
            "1.0.0",
            enabled=True,
            fetcher=failing_fetcher,
        )
    assert "super-secret" not in str(raised.value)
    assert "/Users/alice" not in str(raised.value)


def test_non_bytes_fetcher_response_is_rejected():
    with pytest.raises(UpdateFetchError):
        check_for_update(
            MANIFEST_URL,
            "1.0.0",
            enabled=True,
            fetcher=lambda *_args: "not bytes",
        )
