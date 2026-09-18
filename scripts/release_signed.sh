#!/bin/zsh
# Підписаний і нотаризований реліз DupScan — «одна команда» ПІСЛЯ того,
# як власник один раз налаштує Apple Developer.
#
# Що потрібно від власника РІВНО ОДИН РАЗ (скрипт паролів не питає і не
# зберігає — це принципово):
#   1. Membership Apple Developer Program (developer.apple.com).
#   2. Сертифікат «Developer ID Application» у Keychain.
#   3. Профіль нотаризації в keychain:
#      xcrun notarytool store-credentials dupscan-notary \
#        --apple-id <email> --team-id <TEAMID>
# Далі кожен реліз:
#   DUPSCAN_SIGNING_IDENTITY="Developer ID Application: Ім'я (TEAMID)" \
#   DUPSCAN_NOTARY_PROFILE=dupscan-notary zsh scripts/release_signed.sh
set -euo pipefail

VARIANT_ROOT="${0:A:h:h}"
APP="$VARIANT_ROOT/dist/DupScan"*.app
DMG=("$VARIANT_ROOT"/dist/DupScan-*.dmg)
ENTITLEMENTS="$VARIANT_ROOT/packaging/entitlements.plist"

if [[ -z "${DUPSCAN_SIGNING_IDENTITY:-}" || -z "${DUPSCAN_NOTARY_PROFILE:-}" ]]; then
  cat >&2 <<'HELP'
Не задано ідентичність підпису або профіль нотаризації.

Потрібно (один раз):
  1) Apple Developer Program;
  2) сертифікат "Developer ID Application" у Keychain;
  3) xcrun notarytool store-credentials dupscan-notary \
       --apple-id <email> --team-id <TEAMID>

Потім:
  DUPSCAN_SIGNING_IDENTITY="Developer ID Application: ... (TEAMID)" \
  DUPSCAN_NOTARY_PROFILE=dupscan-notary zsh scripts/release_signed.sh

Скрипт свідомо НЕ приймає паролів і нічого не підписав.
HELP
  exit 2
fi

APP_PATH=$(/bin/ls -d $~APP | head -1)
[[ -d "$APP_PATH" ]] || { echo "Немає зібраного .app у dist/ — спершу zsh scripts/build_macos.sh" >&2; exit 2; }
[[ -f "$ENTITLEMENTS" ]] || { echo "Немає $ENTITLEMENTS" >&2; exit 2; }

echo "== Підпис (hardened runtime): $APP_PATH"
/usr/bin/codesign --force --deep --strict --timestamp \
  --options runtime \
  --entitlements "$ENTITLEMENTS" \
  --sign "$DUPSCAN_SIGNING_IDENTITY" \
  "$APP_PATH"
/usr/bin/codesign --verify --deep --strict "$APP_PATH"

DMG_PATH="${DMG[1]:-}"
[[ -f "$DMG_PATH" ]] || { echo "Немає DMG у dist/" >&2; exit 2; }
echo "== Підпис DMG: $DMG_PATH"
/usr/bin/codesign --force --timestamp --sign "$DUPSCAN_SIGNING_IDENTITY" "$DMG_PATH"

echo "== Нотаризація (submit --wait)"
/usr/bin/xcrun notarytool submit "$DMG_PATH" \
  --keychain-profile "$DUPSCAN_NOTARY_PROFILE" --wait

echo "== Staple + фінальна перевірка Gatekeeper"
/usr/bin/xcrun stapler staple "$APP_PATH"
/usr/bin/xcrun stapler staple "$DMG_PATH" || true  # DMG-staple опційний
/usr/sbin/spctl --assess --type execute --verbose "$APP_PATH"
/usr/bin/shasum -a 256 "$DMG_PATH"
echo "Готово: застосунок пройде Gatekeeper на чужому Mac."
