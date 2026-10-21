#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


def bump_version(text: str, part: str = "patch") -> str:
    match = re.search(r"^version:\s*([0-9]+)\.([0-9]+)\.([0-9]+)\s*$", text, re.M)
    if not match:
        raise SystemExit("Could not find semantic version in config.yaml")

    major, minor, patch = map(int, match.groups())
    if part == "patch":
        patch += 1
    elif part == "minor":
        minor += 1
        patch = 0
    elif part == "major":
        major += 1
        minor = 0
        patch = 0
    else:
        raise SystemExit("part must be one of: patch, minor, major")

    return re.sub(
        r"^version:\s*[0-9]+\.[0-9]+\.[0-9]+\s*$",
        f"version: {major}.{minor}.{patch}",
        text,
        count=1,
        flags=re.M,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Usage: bump-version.py <config.yaml> [patch|minor|major]")

    path = Path(sys.argv[1])
    part = sys.argv[2] if len(sys.argv) > 2 else "patch"
    path.write_text(bump_version(path.read_text(), part))
    print(path.read_text().splitlines()[2])
