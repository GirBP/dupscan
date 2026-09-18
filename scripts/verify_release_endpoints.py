#!/usr/bin/env python3
"""Validate public release endpoints before and after bundle creation."""

from __future__ import annotations

import argparse
import os
import plistlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dupscan.infra.updates import (  # noqa: E402
    BUNDLE_SUPPORT_KEY,
    BUNDLE_UPDATE_MANIFEST_KEY,
    UpdateValidationError,
    validate_https_url,
)

ENDPOINTS = (
    ("DUPSCAN_UPDATE_MANIFEST_URL", BUNDLE_UPDATE_MANIFEST_KEY),
    ("DUPSCAN_SUPPORT_URL", BUNDLE_SUPPORT_KEY),
)


def verify(*, require: bool = False, plist_path: str | None = None) -> int:
    expected: dict[str, str] = {}
    for env_name, key in ENDPOINTS:
        value = os.environ.get(env_name, "")
        if require and not value:
            raise ValueError(f"Production release requires {env_name}")
        if value:
            try:
                expected[key] = validate_https_url(value)
            except UpdateValidationError as error:
                raise ValueError(f"Invalid {env_name}: {error}") from error

    if plist_path is not None:
        with Path(plist_path).open("rb") as handle:
            document = plistlib.load(handle)
        if not isinstance(document, dict):
            raise ValueError("Bundle Info.plist must be a dictionary")
        for _env_name, key in ENDPOINTS:
            bundled = document.get(key)
            wanted = expected.get(key)
            if wanted is None:
                if bundled not in (None, ""):
                    raise ValueError(f"Unexpected bundle endpoint: {key}")
            elif bundled != wanted:
                raise ValueError(f"Bundle endpoint mismatch: {key}")
            if bundled:
                validate_https_url(bundled)
    return len(expected)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require", action="store_true")
    parser.add_argument("--plist")
    args = parser.parse_args(argv)
    try:
        count = verify(require=args.require, plist_path=args.plist)
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise SystemExit(str(error)) from error
    print(f"release-endpoints-ok: {count}")


if __name__ == "__main__":
    main()
