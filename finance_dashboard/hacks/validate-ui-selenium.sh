#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="/data/v2/cache/opencode/finance-dashboard-selenium-venv"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install selenium pillow fastapi uvicorn requests sqlalchemy cryptography >/dev/null

if [ "${FINANCE_DASHBOARD_URL:-}" = "local" ]; then
  exec "$VENV_DIR/bin/python" "$ROOT_DIR/hacks/validate-ui-real.py" --file "$ROOT_DIR/frontend/index.html" "$@"
fi

LIVE_URL="${FINANCE_DASHBOARD_URL:-http://homeassistant:8000/}"
exec "$VENV_DIR/bin/python" "$ROOT_DIR/hacks/validate-ui-real.py" --file "$ROOT_DIR/frontend/index.html" --url "$LIVE_URL" "$@"
