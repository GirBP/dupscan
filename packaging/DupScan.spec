# -*- mode: python ; coding: utf-8 -*-

import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.abspath(SPEC)))
src_root = os.path.join(project_root, "src")
sys.path.insert(0, src_root)
from dupscan.version import (
    BUILD,
    BUNDLE_IDENTIFIER,
    BUNDLE_NAME,
    DISPLAY_NAME,
    VERSION,
)

update_manifest_url = os.environ.get("DUPSCAN_UPDATE_MANIFEST_URL", "")
support_url = os.environ.get("DUPSCAN_SUPPORT_URL", "")
bundle_info = {
    "CFBundleDisplayName": DISPLAY_NAME,
    "CFBundleName": BUNDLE_NAME,
    "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": str(BUILD),
    "LSMinimumSystemVersion": "13.0",
    "LSApplicationCategoryType": "public.app-category.utilities",
    "CFBundlePackageType": "APPL",
    "NSHighResolutionCapable": True,
    "NSHumanReadableCopyright": "Copyright © 2026 DupScan",
}
if update_manifest_url:
    bundle_info["DupScanUpdateManifestURL"] = update_manifest_url
if support_url:
    bundle_info["DupScanSupportURL"] = support_url


a = Analysis(
    [os.path.join(src_root, "dupscan", "ui", "app.py")],
    pathex=[src_root],
    binaries=[],
    datas=[
        (os.path.join(project_root, "docs", "PRIVACY.md"), "."),
        (os.path.join(project_root, "CHANGELOG.md"), "."),
        (os.path.join(project_root, "docs", "THIRD_PARTY_NOTICES.md"), "."),
        (os.path.join(project_root, "licenses"), "licenses"),
    ],
    hiddenimports=[],
    hookspath=[os.path.join(project_root, "packaging", "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    # PySide6/__init__.py contains a Windows-only conditional QtNetwork import
    # that static analysis sees on macOS. DupScan uses urllib for HTTPS and no
    # Qt binary retained below links QtNetwork.
    excludes=["PySide6.QtNetwork"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DupScan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=os.environ.get("DUPSCAN_TARGET_ARCH", "arm64"),
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DupScan",
)
app = BUNDLE(
    coll,
    name=f"{DISPLAY_NAME}.app",
    icon=os.path.join(project_root, "assets", "DupScan.icns"),
    bundle_identifier=BUNDLE_IDENTIFIER,
    info_plist=bundle_info,
)
