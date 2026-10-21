#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="/data/v2/cache/opencode/finance-dashboard-selenium-venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
  "$VENV_DIR/bin/pip" install selenium >/dev/null
fi

exec "$VENV_DIR/bin/python" "$ROOT_DIR/hacks/validate-ui-selenium.py" "$@"
