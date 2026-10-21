#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${1:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"
VERSION_FILE="$ROOT_DIR/config.yaml"
EXPECTED_VERSION="$(python3 - "$VERSION_FILE" <<'PY'
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

"$ROOT_DIR/hacks/refresh-marketplace.sh" "$REPO" "$EXPECTED_VERSION"

echo "Repo release prep complete."
echo "Marketplace metadata now reports $EXPECTED_VERSION."
echo "Next step in Home Assistant: update the add-on with backup disabled, then validate the running UI."
