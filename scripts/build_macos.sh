#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
SRC="$ROOT/src"
PYTHON="${DUPSCAN_PYTHON:-$ROOT/.venv/bin/python}"
PYINSTALLER="${DUPSCAN_PYINSTALLER:-$ROOT/.venv/bin/pyinstaller}"
RUFF="${DUPSCAN_RUFF:-$ROOT/.venv/bin/ruff}"
MYPY="${DUPSCAN_MYPY:-$ROOT/.venv/bin/mypy}"
PYTEST="${DUPSCAN_PYTEST:-$ROOT/.venv/bin/pytest}"
export PYINSTALLER_CONFIG_DIR="${DUPSCAN_PYINSTALLER_CONFIG_DIR:-$ROOT/.pyinstaller-config}"
export PYTHONPATH="$SRC"

cd "$ROOT"

"$PYTHON" "$ROOT/scripts/verify_locked_dependencies.py"
"$PYTHON" -m pip check
if [[ "${DUPSCAN_REQUIRE_PRODUCTION_ENDPOINTS:-0}" == "1" ]]; then
  "$PYTHON" "$ROOT/scripts/verify_release_endpoints.py" --require
else
  "$PYTHON" "$ROOT/scripts/verify_release_endpoints.py"
fi
# Матриця ФС (hdiutil-образи APFS/HFS+/exFAT, включно з повним ланцюгом
# злиття) входить у гейт релізу. Саме її вимкненість дала серію збоїв, які
# доводилося ловити на живому диску власника: exFAT і злиття не перевіряв
# ніхто. Де hdiutil недоступний, ці тести чесно пропускаються самі.
DUPSCAN_FS_MATRIX=1 "$PYTEST" -q
"$RUFF" check "$SRC/dupscan/ui/app.py" "$SRC/dupscan/infra/cache.py" \
  "$SRC/dupscan/domain/clusters.py" "$SRC/dupscan/domain/core.py" \
  "$SRC/dupscan/ui/clusters_tab_controller.py" \
  "$SRC/dupscan/infra/devices.py" "$SRC/dupscan/infra/diagnostics.py" \
  "$SRC/dupscan/ui/perceptual.py" "$SRC/dupscan/ui/perceptual_tab_controller.py" \
  "$SRC/dupscan/infra/preferences.py" \
  "$SRC/dupscan/domain/product.py" "$SRC/dupscan/ui/problems_tab_controller.py" \
  "$SRC/dupscan/infra/removal_history.py" \
  "$SRC/dupscan/ui/folder_comparison_controller.py" \
  "$SRC/dupscan/ui/removal_history_controller.py" \
  "$SRC/dupscan/infra/reports.py" "$SRC/dupscan/ui/reports_controller.py" \
  "$SRC/dupscan/ui/scale_ui.py" "$SRC/dupscan/ui/settings_controller.py" \
  "$SRC/dupscan/infra/session.py" "$SRC/dupscan/infra/storage_guard.py" \
  "$SRC/dupscan/infra/throttle.py" "$SRC/dupscan/infra/updates.py" \
  "$SRC/dupscan/version.py" "$SRC/dupscan/format.py" \
  "$SRC/dupscan/ui/workflow_ui.py" tests
# Клас, розірваний навпіл, ruff і тести самі не ловлять — лише mypy.
# Прагматичний [tool.mypy] у pyproject.toml (ignore_missing_imports,
# check_untyped_defs; strict НЕ увімкнено).
"$MYPY" "$SRC/dupscan/ui/app.py" "$SRC/dupscan/infra/cache.py" \
  "$SRC/dupscan/domain/clusters.py" "$SRC/dupscan/domain/core.py" \
  "$SRC/dupscan/ui/clusters_tab_controller.py" \
  "$SRC/dupscan/infra/devices.py" "$SRC/dupscan/infra/diagnostics.py" \
  "$SRC/dupscan/infra/fsops.py" "$SRC/dupscan/ui/table_models.py" \
  "$SRC/dupscan/ui/perceptual.py" "$SRC/dupscan/ui/perceptual_tab_controller.py" \
  "$SRC/dupscan/infra/preferences.py" \
  "$SRC/dupscan/domain/product.py" "$SRC/dupscan/ui/problems_tab_controller.py" \
  "$SRC/dupscan/infra/removal_history.py" \
  "$SRC/dupscan/ui/folder_comparison_controller.py" \
  "$SRC/dupscan/ui/removal_history_controller.py" \
  "$SRC/dupscan/infra/reports.py" "$SRC/dupscan/ui/reports_controller.py" \
  "$SRC/dupscan/ui/scale_ui.py" "$SRC/dupscan/ui/settings_controller.py" \
  "$SRC/dupscan/infra/session.py" "$SRC/dupscan/infra/storage_guard.py" \
  "$SRC/dupscan/infra/throttle.py" "$SRC/dupscan/infra/updates.py" \
  "$SRC/dupscan/version.py" "$SRC/dupscan/format.py" "$SRC/dupscan/ui/workers.py" \
  "$SRC/dupscan/ui/workflow_ui.py"
"$PYINSTALLER" --clean --noconfirm --workpath .build --distpath dist \
  packaging/DupScan.spec

VERSION=$("$PYTHON" -c 'from dupscan.version import VERSION; print(VERSION)')
BUILD=$("$PYTHON" -c 'from dupscan.version import BUILD; print(BUILD)')
DISPLAY_NAME=$(
  "$PYTHON" -c 'from dupscan.version import DISPLAY_NAME; print(DISPLAY_NAME)'
)
BUNDLE_IDENTIFIER=$(
  "$PYTHON" -c 'from dupscan.version import BUNDLE_IDENTIFIER; print(BUNDLE_IDENTIFIER)'
)
PACKAGE_LABEL=$(
  "$PYTHON" -c 'from dupscan.version import PACKAGE_LABEL; print(PACKAGE_LABEL)'
)
APP="$ROOT/dist/$DISPLAY_NAME.app"
TARGET_ARCH="${DUPSCAN_TARGET_ARCH:-arm64}"

if [[ "${DUPSCAN_REQUIRE_DEVELOPER_ID:-0}" == "1"
      && -z "${DUPSCAN_SIGN_IDENTITY:-}" ]]; then
  echo "Production build requires DUPSCAN_SIGN_IDENTITY" >&2
  exit 2
fi
if [[ "${DUPSCAN_REQUIRE_NOTARIZATION:-0}" == "1"
      && ( -z "${DUPSCAN_NOTARY_PROFILE:-}" || -z "${DUPSCAN_SIGN_IDENTITY:-}" ) ]]; then
  echo "Production notarization requires identity and notary profile" >&2
  exit 2
fi

REQUIRED_RESOURCES=(
  PRIVACY.md
  CHANGELOG.md
  THIRD_PARTY_NOTICES.md
  licenses/BLAKE3-LICENSE.txt
  licenses/IJSON-LICENSE.txt
  licenses/LGPL-3.0.txt
  licenses/OPENSSL-LICENSE.txt
  licenses/PYINSTALLER-COPYING.txt
  licenses/PYTHON-LICENSE.txt
  licenses/SEND2TRASH-LICENSE.txt
  licenses/XZ-0BSD.txt
  licenses/XZ-COPYING.txt
  licenses/XZ-LGPL-2.1.txt
  licenses/ZSTD-LICENSE.txt
)
for resource in $REQUIRED_RESOURCES; do
  if [[ ! -s "$APP/Contents/Resources/$resource" ]]; then
    echo "Missing bundled release resource: $resource" >&2
    exit 2
  fi
done

REQUIRED_QT_PLUGINS=(
  PySide6/Qt/plugins/platforms/libqcocoa.dylib
  PySide6/Qt/plugins/platforms/libqoffscreen.dylib
)
for plugin in $REQUIRED_QT_PLUGINS; do
  if [[ ! -f "$APP/Contents/Frameworks/$plugin" ]]; then
    echo "Missing required Qt platform plugin: $plugin" >&2
    exit 2
  fi
done
FORBIDDEN_QT_PAYLOADS=(
  PySide6/Qt/plugins/imageformats/libqpdf.dylib
  PySide6/Qt/plugins/generic/libqtuiotouchplugin.dylib
  PySide6/Qt/plugins/platforminputcontexts/libqtvirtualkeyboardplugin.dylib
  PySide6/Qt/lib/QtPdf.framework
  PySide6/Qt/lib/QtVirtualKeyboard.framework
  PySide6/Qt/lib/QtVirtualKeyboardQml.framework
  PySide6/QtNetwork.abi3.so
  PySide6/Qt/lib/QtNetwork.framework
  PySide6/Qt/plugins/networkinformation
  PySide6/Qt/plugins/tls
)
for payload in $FORBIDDEN_QT_PAYLOADS; do
  if [[ -e "$APP/Contents/Frameworks/$payload" ]]; then
    echo "Unexpected unused Qt payload: $payload" >&2
    exit 2
  fi
done

PLIST="$APP/Contents/Info.plist"
[[ "$(/usr/bin/plutil -extract CFBundleShortVersionString raw -o - "$PLIST")" == "$VERSION" ]]
[[ "$(/usr/bin/plutil -extract CFBundleVersion raw -o - "$PLIST")" == "$BUILD" ]]
[[ "$(/usr/bin/plutil -extract CFBundleDisplayName raw -o - "$PLIST")" \
    == "$DISPLAY_NAME" ]]
[[ "$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$PLIST")" \
    == "$BUNDLE_IDENTIFIER" ]]
if [[ "${DUPSCAN_REQUIRE_PRODUCTION_ENDPOINTS:-0}" == "1" ]]; then
  "$PYTHON" "$ROOT/scripts/verify_release_endpoints.py" --require --plist "$PLIST"
else
  "$PYTHON" "$ROOT/scripts/verify_release_endpoints.py" --plist "$PLIST"
fi

ARCHS=$(/usr/bin/lipo -archs "$APP/Contents/MacOS/DupScan")
case "$TARGET_ARCH" in
  arm64|x86_64)
    [[ "$ARCHS" == "$TARGET_ARCH" ]] || {
      echo "Expected $TARGET_ARCH binary, got: $ARCHS" >&2
      exit 2
    }
    ;;
  universal2)
    [[ " $ARCHS " == *" arm64 "* && " $ARCHS " == *" x86_64 "* ]] || {
      echo "Expected universal2 binary, got: $ARCHS" >&2
      exit 2
    }
    ;;
  *)
    echo "Unsupported DUPSCAN_TARGET_ARCH: $TARGET_ARCH" >&2
    exit 2
    ;;
esac

if [[ -n "${DUPSCAN_SIGN_IDENTITY:-}" ]]; then
  /usr/bin/codesign --force --deep --strict --options runtime --timestamp \
    --entitlements "$ROOT/packaging/entitlements.plist" \
    --sign "$DUPSCAN_SIGN_IDENTITY" "$APP"
else
  /usr/bin/codesign --force --deep --strict --sign - "$APP"
fi
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"

if [[ "${DUPSCAN_REQUIRE_DEVELOPER_ID:-0}" == "1" ]]; then
  SIGNATURE_INFO=$(/usr/bin/codesign -d --verbose=4 "$APP" 2>&1)
  [[ "$SIGNATURE_INFO" == *"Authority=Developer ID Application:"* ]] || {
    echo "App is not signed with Developer ID Application" >&2
    exit 2
  }
  [[ "$SIGNATURE_INFO" == *"TeamIdentifier="*
      && "$SIGNATURE_INFO" != *"TeamIdentifier=not set"*
      && "$SIGNATURE_INFO" == *"runtime"* ]] || {
    echo "Developer ID build lacks team identity or hardened runtime" >&2
    exit 2
  }
fi

# Exercise queued startup callbacks and the Qt event loop in final signed state.
"$APP/Contents/MacOS/DupScan" --smoke

DMG="$ROOT/dist/DupScan-${DUPSCAN_VERSION_SUFFIX:-$VERSION}-$PACKAGE_LABEL.dmg"
STAGE="$ROOT/dist/.dmg-root"
/bin/rm -rf "$STAGE" "$DMG"
/bin/mkdir -p "$STAGE"
/usr/bin/ditto "$APP" "$STAGE/$DISPLAY_NAME.app"
/bin/ln -s /Applications "$STAGE/Applications"
/usr/bin/hdiutil create -volname "DupScan" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG"
/bin/rm -rf "$STAGE"

if [[ -n "${DUPSCAN_SIGN_IDENTITY:-}" ]]; then
  /usr/bin/codesign --force --timestamp --sign "$DUPSCAN_SIGN_IDENTITY" "$DMG"
  /usr/bin/codesign --verify --strict --verbose=2 "$DMG"
fi

if [[ -n "${DUPSCAN_NOTARY_PROFILE:-}" ]]; then
  if [[ -z "${DUPSCAN_SIGN_IDENTITY:-}" ]]; then
    echo "DUPSCAN_NOTARY_PROFILE requires DUPSCAN_SIGN_IDENTITY" >&2
    exit 2
  fi
  /usr/bin/xcrun notarytool submit "$DMG" --keychain-profile \
    "$DUPSCAN_NOTARY_PROFILE" --wait
  /usr/bin/xcrun stapler staple "$DMG"
  /usr/bin/xcrun stapler validate "$DMG"
  /usr/sbin/spctl --assess --type open --context context:primary-signature \
    --verbose=2 "$DMG"
fi

/usr/bin/hdiutil verify "$DMG"
/usr/bin/shasum -a 256 "$DMG"

echo "Built and verified: $APP"
echo "Distribution image: $DMG"
