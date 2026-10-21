#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/config.yaml"
FRONTEND="$ROOT_DIR/frontend/index.html"
SCREENSHOT="/data/v2/cache/opencode/finance-dashboard-certify.png"

python3 "$ROOT_DIR/hacks/bump-version.py" "$VERSION_FILE" patch >/dev/null
python3 -m py_compile "$ROOT_DIR/backend"/*.py
node "$ROOT_DIR/hacks/validate-ui.mjs" --file "$FRONTEND" --screenshot "$SCREENSHOT"

git -C "$ROOT_DIR" status --short
