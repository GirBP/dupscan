# DupScan update channel

DupScan never checks for updates until the user explicitly selects the update
command. The checker performs a bounded HTTPS `GET` for a UTF-8 JSON manifest
and does not download, execute, or install software. The release page opens
only after a separate user action.

Public builds embed `DUPSCAN_UPDATE_MANIFEST_URL` as
`DupScanUpdateManifestURL` in the signed app `Info.plist`. A frozen app accepts
only that signed value; environment and user-writable preferences cannot
redirect update traffic. Development source runs may use the environment
value. `DUPSCAN_SUPPORT_URL` is handled identically through
`DupScanSupportURL`, and opening it still requires explicit confirmation.

Schema version 1 accepts exactly three required fields and two optional fields:

```json
{
  "schema_version": 1,
  "version": "1.2.0",
  "release_url": "https://dupscan.example/releases/1.2.0",
  "summary": "Optional short release summary.",
  "published_at": "2026-07-22T10:30:00Z"
}
```

- `version` follows Semantic Versioning 2.0.0.
- `release_url` must be HTTPS and cannot contain credentials or a fragment.
- Unknown and duplicate fields are rejected.
- The default timeout is 5 seconds and the default response cap is 64 KiB.
- Redirects to a non-HTTPS destination are rejected before following them.

No persistent update consent is stored: every check starts from the visible
manual command. Failed checks are non-blocking and never weaken scanning or
deletion safety.
