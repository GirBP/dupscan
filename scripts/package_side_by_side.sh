#!/bin/zsh
set -euo pipefail

VARIANT_ROOT="${0:A:h:h}"
REPO_ROOT="${VARIANT_ROOT:h:h}"
OUTPUT_ROOT="$REPO_ROOT/versioned-apps"
stage=""

cleanup_stage() {
  if [[ -n "$stage" && -d "$stage" ]]; then
    /bin/rm -rf "$stage"
  fi
}
trap cleanup_stage EXIT INT TERM

entries=(
  # Консолідація 2026-08-04 (наказ власника): у git і на диску лишається
  # ЛИШЕ фінальна версія. Попередні 20 записів (1.0.0..2.20.0) прибрано —
  # їхні варіанти-джерела переміщені в Кошик, і запис із мертвим шляхом
  # валив би пакування на першому ж кроці (Missing source app).
  "2.21.0|Group Proof|DupScan Group Proof|com.dupscan.app.groupproof|$REPO_ROOT/variants/dupscan-solid-core/dist/DupScan 2.21.0 Group Proof.app"
  # Попередній запис (2.22.0 «Insight», BUILD 31) НЕ видалено — її .app досі живе у versioned-apps, «keep»-гілка
  # вище перевірить і полишить без змін.
  "2.22.0|Insight|DupScan Insight|com.dupscan.app.insight|$REPO_ROOT/variants/dupscan-solid-core/dist/DupScan 2.22.0 Insight.app"
  "2.23.0|Reference|DupScan Reference|com.dupscan.app.reference|$REPO_ROOT/variants/dupscan-solid-core/dist/DupScan 2.23.0 Reference.app"
  "2.24.0|Endurance|DupScan Endurance|com.dupscan.app.endurance|$REPO_ROOT/variants/dupscan-solid-core/dist/DupScan 2.24.0 Endurance.app"
  "2.25.0|Clarity|DupScan Clarity|com.dupscan.app.clarity|$REPO_ROOT/variants/dupscan-solid-core/dist/DupScan 2.25.0 Clarity.app"
  "2.26.0|Mend|DupScan Mend|com.dupscan.app.mend|$REPO_ROOT/variants/dupscan-solid-core/dist/DupScan 2.26.0 Mend.app"
)

/bin/mkdir -p "$OUTPUT_ROOT"

for entry in $entries; do
  IFS="|" read -r version label bundle_name bundle_id source_app <<< "$entry"
  display_name="DupScan $version $label"
  package_label="${label// /-}"
  output_app="$OUTPUT_ROOT/$display_name.app"
  output_dmg="$OUTPUT_ROOT/DupScan-$version-$package_label.dmg"
  plist="$output_app/Contents/Info.plist"

  if [[ ! -d "$source_app" ]]; then
    echo "Missing source app: $source_app" >&2
    exit 2
  fi
  actual_version=$(
    /usr/bin/plutil -extract CFBundleShortVersionString raw -o - \
      "$source_app/Contents/Info.plist"
  )
  if [[ "$actual_version" != "$version" ]]; then
    echo "Expected $version in $source_app, got $actual_version" >&2
    exit 2
  fi

  # Та сама VERSION з іншим BUILD — це ІНШИЙ бандл. Keep-гілка нижче
  # звіряла лише VERSION та імена, тому перезбірка тієї самої версії тихо
  # лишала стару збірку: 2.19.0 BUILD 23 пережила BUILD 24 і поїхала б до
  # власника застарілою. Застаріле — у Кошик, ніколи не rm.
  actual_build=$(
    /usr/bin/plutil -extract CFBundleVersion raw -o - \
      "$source_app/Contents/Info.plist"
  )
  if [[ -d "$output_app" && -f "$plist" ]]; then
    packaged_build=$(
      /usr/bin/plutil -extract CFBundleVersion raw -o - "$plist" 2>/dev/null \
        || echo "?"
    )
    if [[ "$packaged_build" != "$actual_build" ]]; then
      echo "Superseded $display_name: build $packaged_build → $actual_build (у Кошик)"
      /usr/bin/osascript -e \
        "tell application \"Finder\" to delete POSIX file \"$output_app\"" \
        > /dev/null
      if [[ -f "$output_dmg" ]]; then
        /usr/bin/osascript -e \
          "tell application \"Finder\" to delete POSIX file \"$output_dmg\"" \
          > /dev/null
      fi
    fi
  fi

  if [[ -d "$output_app" ]]; then
    [[ "$(/usr/bin/plutil -extract CFBundleDisplayName raw -o - "$plist")" \
        == "$display_name" ]]
    [[ "$(/usr/bin/plutil -extract CFBundleName raw -o - "$plist")" \
        == "$bundle_name" ]]
    [[ "$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$plist")" \
        == "$bundle_id" ]]
    [[ "$(/usr/bin/plutil -extract CFBundleShortVersionString raw -o - "$plist")" \
        == "$version" ]]
    /usr/bin/codesign --verify --deep --strict "$output_app"
    "$output_app/Contents/MacOS/DupScan" --smoke
    if [[ -f "$output_dmg" ]]; then
      /usr/bin/hdiutil verify "$output_dmg"
      echo "Kept existing package: $display_name"
      /usr/bin/shasum -a 256 "$output_dmg"
      continue
    fi
    if [[ -e "$output_dmg" ]]; then
      echo "Invalid existing DMG target for $display_name; refusing to overwrite" >&2
      exit 2
    fi
    echo "Completing missing DMG for verified app: $display_name"
  elif [[ -e "$output_app" || -e "$output_dmg" ]]; then
    echo "Incomplete existing package for $display_name; refusing to overwrite" >&2
    exit 2
  else
    /usr/bin/ditto "$source_app" "$output_app"
    /usr/bin/plutil -replace CFBundleDisplayName -string "$display_name" "$plist"
    /usr/bin/plutil -replace CFBundleName -string "$bundle_name" "$plist"
    /usr/bin/plutil -replace CFBundleIdentifier -string "$bundle_id" "$plist"

    /usr/bin/codesign --force --deep --strict --sign - "$output_app"
    /usr/bin/codesign --verify --deep --strict "$output_app"
    "$output_app/Contents/MacOS/DupScan" --smoke
  fi

  stage=$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/dupscan-side-by-side.XXXXXX")
  # Materialize a regular bundle so hdiutil never depends on clone support
  # of the source/destination filesystem.
  /usr/bin/ditto "$output_app" "$stage/$display_name.app"
  /bin/ln -s /Applications "$stage/Applications"
  /usr/bin/hdiutil create -volname "$display_name" -srcfolder "$stage" \
    -ov -format UDZO "$output_dmg"
  /bin/rm -rf "$stage"
  stage=""
  /usr/bin/hdiutil verify "$output_dmg"

  [[ "$(/usr/bin/plutil -extract CFBundleDisplayName raw -o - "$plist")" \
      == "$display_name" ]]
  [[ "$(/usr/bin/plutil -extract CFBundleName raw -o - "$plist")" \
      == "$bundle_name" ]]
  [[ "$(/usr/bin/plutil -extract CFBundleIdentifier raw -o - "$plist")" \
      == "$bundle_id" ]]
  [[ "$(/usr/bin/plutil -extract CFBundleShortVersionString raw -o - "$plist")" \
      == "$version" ]]
  /usr/bin/shasum -a 256 "$output_dmg"
done

# Число рахується, а не зашивається: зашите "eighteen" пережило дві версії
# і почало брехати.
echo "Created ${#entries[@]} isolated install names in: $OUTPUT_ROOT"
