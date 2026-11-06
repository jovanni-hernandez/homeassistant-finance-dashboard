#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
for env_file in "$ROOT_DIR/.env" "$ROOT_DIR/hacks/.release.env"; do
  if [ -f "$env_file" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$env_file"
    set +a
  fi
done

ARTIFACT_DIR="${FINANCE_TEST_ARTIFACT_DIR:-/data/v2/cache/opencode}"
if ! mkdir -p "$ARTIFACT_DIR" 2>/dev/null || [ ! -w "$ARTIFACT_DIR" ]; then
  ARTIFACT_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/opencode"
  mkdir -p "$ARTIFACT_DIR"
fi
VENV_DIR="${FINANCE_TEST_VENV:-$ARTIFACT_DIR/finance-dashboard-selenium-venv}"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi
if ! "$VENV_DIR/bin/python" -c 'import selenium' >/dev/null 2>&1; then
  "$VENV_DIR/bin/pip" install selenium >/dev/null
fi

exec "$VENV_DIR/bin/python" "$ROOT_DIR/hacks/finance-browser.py" "$@"
