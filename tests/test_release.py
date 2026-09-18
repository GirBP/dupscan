"""Release metadata and reproducible-build contract."""

import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dupscan.ui.app as app  # noqa: E402
from dupscan.version import (  # noqa: E402
    BUILD,
    BUNDLE_IDENTIFIER,
    BUNDLE_NAME,
    DISPLAY_NAME,
    PACKAGE_LABEL,
    VARIANT_BADGE,
    VARIANT_NAME,
    VERSION,
)


def test_release_version_has_single_declared_value():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["version"] == VERSION == app.__version__
    assert BUILD >= 1
    spec = (ROOT / "packaging/DupScan.spec").read_text()
    assert "BUILD," in spec and "VERSION," in spec
    assert '"CFBundleShortVersionString": VERSION' in spec


def test_release_identity_has_one_authoritative_source():
    assert DISPLAY_NAME == f"DupScan {VERSION} {VARIANT_NAME}"
    assert BUNDLE_NAME == f"DupScan {VARIANT_NAME}"
    assert VARIANT_BADGE == VARIANT_NAME.upper()
    assert BUNDLE_IDENTIFIER == "com.dupscan.app.mend"
    assert PACKAGE_LABEL == "Mend"
    spec = (ROOT / "packaging/DupScan.spec").read_text()
    for name in (
        "BUNDLE_IDENTIFIER",
        "BUNDLE_NAME",
        "DISPLAY_NAME",
        "VERSION",
    ):
        assert name in spec
    script = (ROOT / "scripts/build_macos.sh").read_text()
    for name in ("BUNDLE_IDENTIFIER", "DISPLAY_NAME", "PACKAGE_LABEL"):
        assert f"from dupscan.version import {name}" in script


def test_runtime_ui_contains_no_previous_variant_literal():
    source = (ROOT / "src/dupscan/ui/app.py").read_text()
    for stale in (
        "Identity Guard",
        "IDENTITY GUARD",
        "Storage Guard",
        "STORAGE GUARD",
        "Release Guard",
        "RELEASE GUARD",
        "Visual Integrity",
        "VISUAL INTEGRITY",
        "Group Proof",
        "GROUP PROOF",
        "Insight",
        "INSIGHT",
        "Reference",
        "REFERENCE",
        "Endurance",
        "ENDURANCE",
        "Clarity",
        "CLARITY",
    ):
        assert stale not in source
    for identity in ("DISPLAY_NAME", "VARIANT_BADGE", "VARIANT_NAME"):
        assert identity in source


def test_locked_runtime_dependencies_are_declared():
    lock = (ROOT / "requirements.lock").read_text().casefold()
    for package in (
        "pyside6==", "blake3==", "ijson==", "send2trash==", "pyinstaller=="
    ):
        assert package in lock


def test_release_files_and_executable_build_script_exist():
    for name in ("README.md", "CHANGELOG.md"):
        assert (ROOT / name).is_file()
    for name in ("PRIVACY.md", "THIRD_PARTY_NOTICES.md", "RELEASE_CHECKLIST.md"):
        assert (ROOT / "docs" / name).is_file()
    script = ROOT / "scripts/build_macos.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    icon = ROOT / "assets/DupScan.icns"
    assert icon.is_file() and icon.stat().st_size > 100_000
    assert 'icon=os.path.join(project_root, "assets", "DupScan.icns")' in (
        ROOT / "packaging/DupScan.spec"
    ).read_text()


def test_runtime_license_texts_are_nonempty_and_bundled_by_spec():
    expected = {
        "BLAKE3-LICENSE.txt",
        "IJSON-LICENSE.txt",
        "LGPL-3.0.txt",
        "OPENSSL-LICENSE.txt",
        "PYINSTALLER-COPYING.txt",
        "PYTHON-LICENSE.txt",
        "SEND2TRASH-LICENSE.txt",
        "XZ-0BSD.txt",
        "XZ-COPYING.txt",
        "XZ-LGPL-2.1.txt",
        "ZSTD-LICENSE.txt",
    }
    license_dir = ROOT / "licenses"
    assert {path.name for path in license_dir.iterdir() if path.is_file()} == expected
    assert all((license_dir / name).stat().st_size >= 500 for name in expected)
    spec = (ROOT / "packaging/DupScan.spec").read_text()
    assert '(os.path.join(project_root, "licenses"), "licenses")' in spec
    notices = (ROOT / "docs" / "THIRD_PARTY_NOTICES.md").read_text()
    assert all(f"`licenses/{name}`" in notices for name in expected)


def test_release_script_has_final_artifact_gates():
    script = (ROOT / "scripts/build_macos.sh").read_text()
    for contract in (
            'hdiutil verify "$DMG"',
            'shasum -a 256 "$DMG"',
            "CFBundleShortVersionString",
            "CFBundleIdentifier",
            "DUPSCAN_REQUIRE_DEVELOPER_ID",
            "DUPSCAN_REQUIRE_NOTARIZATION",
            "DUPSCAN_REQUIRE_PRODUCTION_ENDPOINTS",
            "verify_release_endpoints.py",
            '/usr/bin/lipo -archs "$APP/Contents/MacOS/DupScan"',
            '"$APP/Contents/MacOS/DupScan" --smoke'):
        assert contract in script
    verifier = (ROOT / "scripts/verify_release_endpoints.py").read_text()
    assert "BUNDLE_UPDATE_MANIFEST_KEY" in verifier
    assert "BUNDLE_SUPPORT_KEY" in verifier


def test_side_by_side_packaging_materializes_and_cleans_its_stage():
    script = (ROOT / "scripts/package_side_by_side.sh").read_text()
    assert '/usr/bin/ditto "$output_app" "$stage/$display_name.app"' in script
    assert "trap cleanup_stage EXIT INT TERM" in script
    assert 'stage=""' in script


def test_smoke_mode_pumps_a_bounded_event_loop():
    source = (ROOT / "src/dupscan/ui/app.py").read_text()
    assert "QTimer.singleShot(200, app.quit)" in source
    assert "code = app.exec()" in source


def test_bundle_spec_embeds_only_public_release_endpoints():
    spec = (ROOT / "packaging/DupScan.spec").read_text()
    assert 'os.environ.get("DUPSCAN_UPDATE_MANIFEST_URL", "")' in spec
    assert 'os.environ.get("DUPSCAN_SUPPORT_URL", "")' in spec
    assert 'bundle_info["DupScanUpdateManifestURL"]' in spec
    assert 'bundle_info["DupScanSupportURL"]' in spec


def test_release_script_runs_mypy_gate_beside_ruff():
    """Клас, розірваний навпіл,
    ruff і тести мовчали, спіймав лише mypy. Гейт збірки мусить кликати mypy
    поруч із ruff на тих самих продуктових модулях, ДО pyinstaller."""
    script = (ROOT / "scripts/build_macos.sh").read_text()
    assert 'MYPY="${DUPSCAN_MYPY:-$ROOT/.venv/bin/mypy}"' in script
    ruff_pos = script.index('"$RUFF" check')
    mypy_pos = script.index('"$MYPY"')
    pyinstaller_pos = script.index('"$PYINSTALLER" --clean')
    assert ruff_pos < mypy_pos < pyinstaller_pos
    for module in (
        "app.py", "cache.py", "clusters.py", "core.py", "devices.py",
        "diagnostics.py", "fsops.py", "table_models.py", "perceptual.py",
        "preferences.py", "product.py", "removal_history.py", "reports.py",
        "scale_ui.py", "session.py", "storage_guard.py", "throttle.py",
        "updates.py", "version.py", "format.py", "workers.py", "workflow_ui.py",
    ):
        assert module in script[mypy_pos:pyinstaller_pos]


def test_qt_bundle_filter_is_narrow_and_keeps_platform_smoke_plugins():
    spec = (ROOT / "packaging/DupScan.spec").read_text()
    assert '"packaging", "hooks"' in spec
    assert 'excludes=["PySide6.QtNetwork"]' in spec
    hook = (ROOT / "packaging/hooks/hook-PySide6.QtGui.py").read_text()
    assert "_EXCLUDED_PLUGINS" in hook
    assert "libqpdf.dylib" in hook
    assert "libqtuiotouchplugin.dylib" in hook
    assert "libqtvirtualkeyboardplugin.dylib" in hook
    assert "libqcocoa.dylib" not in hook
    assert "libqoffscreen.dylib" not in hook
    script = (ROOT / "scripts/build_macos.sh").read_text()
    assert "REQUIRED_QT_PLUGINS" in script
    assert "FORBIDDEN_QT_PAYLOADS" in script


def test_signed_release_script_is_notarization_ready():
    """Конвеєр «одна команда після сертифіката».
    Скрипт НЕ приймає паролів (профіль notarytool створює власник сам) і без
    env-ідентичності мусить відмовити, нічого не підписавши."""
    script_path = ROOT / "scripts/release_signed.sh"
    assert script_path.is_file() and os.access(script_path, os.X_OK)
    script = script_path.read_text()
    for contract in (
            "set -euo pipefail",
            "DUPSCAN_SIGNING_IDENTITY",
            "DUPSCAN_NOTARY_PROFILE",
            "--options runtime",
            "--entitlements",
            "notarytool submit",
            "--keychain-profile",
            "stapler staple",
            "spctl --assess",
    ):
        assert contract in script, contract
    assert "password" not in script.casefold()
    entitlements = (ROOT / "packaging/entitlements.plist").read_text()
    assert "com.apple.security.cs.disable-library-validation" in entitlements
