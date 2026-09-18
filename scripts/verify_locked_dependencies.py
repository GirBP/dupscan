#!/usr/bin/env python3
"""Fail when build-environment package versions drift from requirements.lock."""

from __future__ import annotations

from importlib import metadata
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    requirements = root / "requirements.lock"
    checked: list[str] = []
    for number, raw in enumerate(requirements.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise SystemExit(f"{requirements}:{number}: expected exact name==version pin")
        name, expected = (part.strip() for part in line.split("==", 1))
        if not name or not expected:
            raise SystemExit(f"{requirements}:{number}: incomplete exact pin")
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as error:
            raise SystemExit(f"Missing locked dependency: {name}=={expected}") from error
        if actual != expected:
            raise SystemExit(
                f"Locked dependency mismatch: {name}=={expected}, installed {actual}")
        checked.append(f"{name}=={actual}")
    if not checked:
        raise SystemExit("requirements.lock contains no exact dependencies")
    print(f"locked-dependencies-ok: {len(checked)}")


if __name__ == "__main__":
    main()
