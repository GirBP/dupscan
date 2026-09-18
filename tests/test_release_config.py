"""Signed endpoint configuration and product UI contracts."""

import os
import plistlib
import sys
import tempfile
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DUPSCAN_DATA_DIR", tempfile.mkdtemp())

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import dupscan.ui.app as app  # noqa: E402
import dupscan.infra.updates as updates  # noqa: E402
from scripts import verify_release_endpoints  # noqa: E402

_qapp = QApplication.instance() or QApplication([])

MANIFEST_URL = "https://updates.dupscan.example/manifest-v1.json"
SUPPORT_URL = "https://support.dupscan.example/help"


def _bundle_plist(tmp_path, document: object):
    contents = tmp_path / "DupScan.app" / "Contents"
    executable = contents / "MacOS" / "DupScan"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(document, handle)
    return executable, contents / "Info.plist"


def test_frozen_configuration_uses_only_signed_bundle_plist(tmp_path, monkeypatch):
    executable, _plist = _bundle_plist(tmp_path, {
        updates.BUNDLE_UPDATE_MANIFEST_KEY: MANIFEST_URL,
        updates.BUNDLE_SUPPORT_KEY: SUPPORT_URL,
    })
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.sys, "executable", str(executable))
    monkeypatch.setenv("DUPSCAN_UPDATE_MANIFEST_URL", "https://evil.example/a")
    monkeypatch.setenv("DUPSCAN_SUPPORT_URL", "https://evil.example/b")

    assert updates.configured_manifest_url() == MANIFEST_URL
    assert updates.configured_support_url() == SUPPORT_URL


def test_missing_malformed_or_invalid_bundle_endpoints_fail_closed(
        tmp_path, monkeypatch):
    executable, plist_path = _bundle_plist(tmp_path, {
        updates.BUNDLE_UPDATE_MANIFEST_KEY: "http://unsafe.example/manifest",
        updates.BUNDLE_SUPPORT_KEY: 123,
    })
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.sys, "executable", str(executable))
    assert updates.configured_manifest_url() is None
    assert updates.configured_support_url() is None

    plist_path.write_bytes(b"not a plist")
    assert updates.configured_manifest_url() is None
    plist_path.unlink()
    assert updates.configured_support_url() is None


def test_development_configuration_accepts_only_valid_https(monkeypatch):
    monkeypatch.setattr(updates.sys, "frozen", False, raising=False)
    monkeypatch.setenv("DUPSCAN_UPDATE_MANIFEST_URL", MANIFEST_URL)
    monkeypatch.setenv("DUPSCAN_SUPPORT_URL", "file:///tmp/support")
    assert updates.configured_manifest_url() == MANIFEST_URL
    assert updates.configured_support_url() is None


def test_release_endpoint_preflight_requires_and_matches_bundle(
        tmp_path, monkeypatch):
    _executable, plist_path = _bundle_plist(tmp_path, {
        updates.BUNDLE_UPDATE_MANIFEST_KEY: MANIFEST_URL,
        updates.BUNDLE_SUPPORT_KEY: SUPPORT_URL,
    })
    monkeypatch.setenv("DUPSCAN_UPDATE_MANIFEST_URL", MANIFEST_URL)
    monkeypatch.setenv("DUPSCAN_SUPPORT_URL", SUPPORT_URL)
    assert verify_release_endpoints.verify(
        require=True, plist_path=str(plist_path)) == 2

    monkeypatch.setenv(
        "DUPSCAN_SUPPORT_URL", "https://support.dupscan.example/different")
    try:
        verify_release_endpoints.verify(require=True, plist_path=str(plist_path))
    except ValueError as error:
        assert "mismatch" in str(error)
    else:
        raise AssertionError("mismatched signed endpoint must fail")


def test_release_endpoint_preflight_rejects_missing_or_unsafe(monkeypatch):
    monkeypatch.delenv("DUPSCAN_UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("DUPSCAN_SUPPORT_URL", raising=False)
    try:
        verify_release_endpoints.verify(require=True)
    except ValueError as error:
        assert "requires DUPSCAN_UPDATE_MANIFEST_URL" in str(error)
    else:
        raise AssertionError("production endpoints must be required")

    monkeypatch.setenv("DUPSCAN_UPDATE_MANIFEST_URL", "http://unsafe.example/a")
    monkeypatch.setenv("DUPSCAN_SUPPORT_URL", SUPPORT_URL)
    try:
        verify_release_endpoints.verify(require=True)
    except ValueError as error:
        assert "Invalid DUPSCAN_UPDATE_MANIFEST_URL" in str(error)
    else:
        raise AssertionError("unsafe endpoint must fail")


def test_update_ui_uses_validated_release_configuration(monkeypatch):
    main = app.Main()
    monkeypatch.setattr(
        updates, "configured_manifest_url", lambda: MANIFEST_URL)
    calls = []

    def fake_check(url, version, *, enabled):
        calls.append((url, version, enabled))
        return SimpleNamespace(update_available=False)

    monkeypatch.setattr(updates, "check_for_update", fake_check)
    monkeypatch.setattr(
        main, "_ui_bg_run",
        lambda job, done, _failed=None: done(job()))
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: messages.append((title, text)))

    main.check_updates()

    assert calls == [(MANIFEST_URL, app.__version__, True)]
    assert messages and "актуальна" in messages[-1][1]
    main.deleteLater()


def test_unconfigured_update_ignores_user_writable_preference(monkeypatch):
    main = app.Main()
    main._settings.setValue(
        "updates/manifest_url", "https://evil.example/user-preference")
    monkeypatch.setattr(updates, "configured_manifest_url", lambda: None)
    monkeypatch.setattr(
        main, "_ui_bg_run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("network attempted")))
    messages = []
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda _parent, title, text: messages.append((title, text)))

    main.check_updates()

    assert messages and "не прив’язано" in messages[-1][1]
    main.deleteLater()


def test_support_url_requires_confirmation_and_opens_exact_https(monkeypatch):
    main = app.Main()
    monkeypatch.setattr(updates, "configured_support_url", lambda: SUPPORT_URL)
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes)
    opened = []
    monkeypatch.setattr(
        app.subprocess, "Popen",
        lambda argv, **_kwargs: opened.append(argv))

    main.show_support()

    assert opened == [["open", SUPPORT_URL]]
    main.deleteLater()
