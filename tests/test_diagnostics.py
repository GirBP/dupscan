"""Privacy, integrity, and size tests for diagnostics export."""

import json
import os
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dupscan.infra.diagnostics as diagnostics
from dupscan.infra.diagnostics import (  # noqa: E402
    DiagnosticsError,
    export_diagnostics_bundle,
    redact_sensitive_text,
)


def read_bundle(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        values = {name: archive.read(name) for name in names}
    return names, values


def test_default_export_redacts_paths_credentials_email_and_binary_data(tmp_path):
    destination = tmp_path / "diagnostics.zip"
    raw_path = "/Users/alice/Documents/Client Secrets/report.pdf"
    result = export_diagnostics_bundle(
        destination,
        logs=[
            f"Failed to scan {raw_path}",
            "token=top-secret contact alice@example.com",
            r"Windows path C:\Users\Alice\Private\notes.txt",
        ],
        errors=[{"path": raw_path, "message": f"Unreadable: {raw_path}"}],
        settings={
            "scan_root": Path(raw_path),
            "api_key": "should-never-appear",
            "normal": "safe",
            "blob": b"PRIVATE FILE CONTENT",
        },
        scanned_paths=[raw_path],
    )

    names, entries = read_bundle(destination)
    assert names == ["metadata.json", "logs.txt", "errors.json", "settings.json"]
    all_text = b"\n".join(entries.values()).decode("utf-8")
    for private_value in (
        raw_path,
        "/Users/alice",
        "top-secret",
        "alice@example.com",
        "should-never-appear",
        "PRIVATE FILE CONTENT",
        r"C:\Users\Alice",
    ):
        assert private_value not in all_text
    assert "<PATH>" in all_text
    assert "<REDACTED>" in all_text
    assert "<EMAIL>" in all_text
    assert "<binary-data:20-bytes-omitted>" in all_text

    metadata = json.loads(entries["metadata.json"])
    assert metadata["application"]["name"] == "DupScan"
    assert metadata["platform"]["system"]
    assert metadata["privacy"] == {
        "file_contents_included": False,
        "paths_in_logs_errors_settings_redacted": True,
        "scanned_paths_included": False,
    }
    assert metadata["scanned_path_count"] == 1
    assert not result.included_scanned_paths
    assert result.bytes_written == destination.stat().st_size


def test_raw_scanned_paths_require_explicit_opt_in_and_do_not_disable_other_redaction(tmp_path):
    raw_path = "/Volumes/Backup/Customers/Acme/archive.zip"
    destination = tmp_path / "with-paths.zip"
    result = export_diagnostics_bundle(
        destination,
        logs=f"Scan warning at {raw_path}",
        errors={"path": raw_path},
        settings={"root": raw_path},
        scanned_paths=[raw_path],
        include_scanned_paths=True,
    )
    _, entries = read_bundle(destination)

    assert json.loads(entries["scanned_paths.json"]) == [raw_path]
    assert raw_path not in entries["logs.txt"].decode()
    assert raw_path not in entries["errors.json"].decode()
    assert raw_path not in entries["settings.json"].decode()
    metadata = json.loads(entries["metadata.json"])
    assert metadata["privacy"]["scanned_paths_included"] is True
    assert metadata["privacy"]["file_contents_included"] is False
    assert result.included_scanned_paths


def test_export_never_reads_files_referenced_by_scanned_paths(tmp_path):
    scanned_file = tmp_path / "scan-me.txt"
    unique_content = "CONTENT-MUST-NEVER-ENTER-DIAGNOSTICS-123456"
    scanned_file.write_text(unique_content)
    destination = tmp_path / "diagnostics.zip"
    export_diagnostics_bundle(
        destination,
        scanned_paths=[scanned_file],
        include_scanned_paths=True,
    )
    _, entries = read_bundle(destination)
    assert str(scanned_file) in entries["scanned_paths.json"].decode()
    assert all(unique_content not in value.decode("utf-8") for value in entries.values())


def test_archive_is_size_bounded_and_reports_truncated_log(tmp_path):
    destination = tmp_path / "large.zip"
    result = export_diagnostics_bundle(destination, logs="абв" * diagnostics.MAX_LOG_BYTES)
    _, entries = read_bundle(destination)
    assert len(entries["logs.txt"]) <= diagnostics.MAX_LOG_BYTES
    assert "truncated" in entries["logs.txt"].decode()
    assert "logs.txt" in result.truncated_entries
    assert destination.stat().st_size <= diagnostics.MAX_ARCHIVE_BYTES
    metadata = json.loads(entries["metadata.json"])
    assert "logs.txt" in metadata["truncated_entries"]


def test_oversized_structured_entries_remain_valid_json(tmp_path):
    destination = tmp_path / "structured.zip"
    huge = {f"item-{index}": "x" * 1000 for index in range(1000)}
    result = export_diagnostics_bundle(destination, errors=huge, settings=huge)
    _, entries = read_bundle(destination)
    errors = json.loads(entries["errors.json"])
    settings = json.loads(entries["settings.json"])
    assert errors["truncated"] is True
    assert settings["truncated"] is True
    assert set(result.truncated_entries) == {"errors.json", "settings.json"}


def test_export_replaces_destination_atomically_with_private_permissions(tmp_path):
    destination = tmp_path / "diagnostics.zip"
    destination.write_bytes(b"old bundle")
    export_diagnostics_bundle(destination, logs="new")
    with zipfile.ZipFile(destination) as archive:
        assert archive.read("logs.txt") == b"new"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".diagnostics.zip.*.tmp"))


def test_failed_write_preserves_existing_destination_and_cleans_temp(tmp_path, monkeypatch):
    destination = tmp_path / "diagnostics.zip"
    destination.write_bytes(b"known-good-existing-bundle")

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated full disk")

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_write)
    with pytest.raises(DiagnosticsError):
        export_diagnostics_bundle(destination, logs="replacement")
    assert destination.read_bytes() == b"known-good-existing-bundle"
    assert not list(tmp_path.glob(".diagnostics.zip.*.tmp"))


@pytest.mark.parametrize(
    "text,private",
    [
        ("open file:///Users/alice/private.txt now", "/Users/alice/private.txt"),
        ("open ~/Documents/private.txt now", "~/Documents/private.txt"),
        ("open /opt/company/private.txt now", "/opt/company/private.txt"),
        (r"open \\server\share\private.txt now", r"\\server\share\private.txt"),
        ("Authorization: Bearer abc.def.secret", "abc.def.secret"),
        ("password = hunter2", "hunter2"),
    ],
)
def test_text_redactor_removes_common_sensitive_shapes(text, private):
    assert private not in redact_sensitive_text(text)


def test_cycles_and_hostile_objects_are_handled_without_calling_user_string_code(tmp_path):
    class Hostile:
        def __str__(self):
            raise AssertionError("untrusted __str__ must not run")

        def __repr__(self):
            raise AssertionError("untrusted __repr__ must not run")

    cyclic = []
    cyclic.append(cyclic)
    destination = tmp_path / "safe.zip"
    export_diagnostics_bundle(
        destination,
        errors=[Hostile(), cyclic],
        settings={Hostile(): Hostile()},
    )
    _, entries = read_bundle(destination)
    combined = entries["errors.json"] + entries["settings.json"]
    assert b"<cycle>" in combined
    assert b"unsupported" in combined


@pytest.mark.parametrize(
    "kwargs",
    [
        {"destination": "missing/diagnostics.zip"},
        {"destination": "."},
        {"destination": "out.zip", "build": True},
        {"destination": "out.zip", "app_version": "/Users/alice/private"},
        {"destination": "out.zip", "include_scanned_paths": 1},
        {"destination": "out.zip", "settings": ["not", "a", "mapping"]},
    ],
)
def test_invalid_export_contract_is_rejected(tmp_path, kwargs):
    destination = kwargs.pop("destination")
    if destination == "out.zip":
        destination = tmp_path / destination
    elif destination == ".":
        destination = tmp_path
    else:
        destination = tmp_path / destination
    with pytest.raises(DiagnosticsError):
        export_diagnostics_bundle(destination, **kwargs)


def test_raw_path_list_rejects_bytes_and_nul(tmp_path):
    with pytest.raises(DiagnosticsError):
        export_diagnostics_bundle(
            tmp_path / "bytes.zip",
            scanned_paths=[b"/private/file"],
            include_scanned_paths=True,
        )
    with pytest.raises(DiagnosticsError):
        export_diagnostics_bundle(
            tmp_path / "nul.zip",
            scanned_paths=["/private/file\x00hidden"],
            include_scanned_paths=True,
        )
