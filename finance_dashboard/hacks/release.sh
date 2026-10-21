#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"

"$ROOT_DIR/hacks/certify.sh"

VERSION="$(python3 - "$ROOT_DIR/config.yaml" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text()
match = re.search(r'^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$', text, re.M)
if not match:
    raise SystemExit('Could not determine version from config.yaml')
print(match.group(1))
PY
)"

git -C "$ROOT_DIR" add -A

if ! git -C "$ROOT_DIR" diff --cached --quiet; then
  git -C "$ROOT_DIR" commit -m "Release v$VERSION"
  git -C "$ROOT_DIR" push
else
  echo "No changes to commit after certification."
fi

"$ROOT_DIR/hacks/deploy-and-verify.sh" "$REPO"
