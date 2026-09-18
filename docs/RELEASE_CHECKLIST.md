# Release checklist

Checked before each tagged release; check state is per-release, not stored
here between releases.

- [ ] `pytest -q` and `ruff check` pass in the project-owned virtualenv.
- [ ] Dependency versions match `requirements.lock`.
- [ ] `version.py`, `pyproject.toml` and bundle Info.plist versions match.
- [ ] `scripts/build_macos.sh` completes and produces both `.app` and DMG.
- [ ] Frozen binary reports `smoke-ok` and has the expected architecture.
- [ ] `codesign --verify --deep --strict` passes.
- [ ] Complete third-party license texts are bundled for distribution.
- [ ] Developer ID signature and hardened-runtime entitlements validate.
- [ ] DMG notarization, stapling and Gatekeeper `spctl` assessment pass.
- [ ] The chosen architecture is tested (`arm64`; ideally `universal2`).
- [ ] An isolated local acceptance fixture covers exact files/folders,
  similarity, session round-trip, verified merge, reports, diagnostics,
  Trash history and conservative restore without personal files or real Trash.
- [ ] Scan storage preflight covers sufficient, low, critical, unavailable
  volume, duplicate-click and stale/closing lifecycle cases.
- [ ] Visible release identity is sourced centrally and scan/results,
  comparison and low-storage screens have a synthetic-data Qt render audit.
- [ ] Full build, side-by-side package and mounted-DMG smoke pass.
- [ ] Duplicate-file, duplicate-folder, similarity and Trash flows are manually
  tested on APFS and one removable volume.
- [ ] `docs/PRIVACY.md` and `CHANGELOG.md` are included with the distribution.
- [ ] HTTPS update manifest and release page use the exact shipped SemVer.
- [ ] First launch, keyboard navigation, VoiceOver labels, light/dark mode and
  200% display scaling are manually checked on a clean macOS account.
- [ ] Update, diagnostics, report export and Trash restore are manually tested.
- [ ] Support URL/contact and Qt/LGPL (or commercial Qt) obligations are final.
