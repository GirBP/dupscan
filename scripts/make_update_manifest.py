#!/usr/bin/env python3
"""Generate and self-validate DupScan's small HTTPS release manifest."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from dupscan.infra.updates import parse_update_manifest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--summary", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    document = {
        "schema_version": 1,
        "version": args.version,
        "release_url": args.release_url,
        "published_at": args.published_at,
    }
    if args.summary:
        document["summary"] = args.summary
    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
               + "\n").encode()
    parse_update_manifest(payload)
    destination = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".update-manifest-", dir=os.path.dirname(destination))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


if __name__ == "__main__":
    main()
