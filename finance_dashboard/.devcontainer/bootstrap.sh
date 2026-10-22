#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
if [ -z "$PYTHON_BIN" ]; then
  echo "Python interpreter not found" >&2
  exit 1
fi

echo "== Finance Dashboard bootstrap =="
echo "Python: $($PYTHON_BIN --version)"

echo "-- Checking backend Python syntax --"
$PYTHON_BIN -m py_compile backend/*.py

echo "-- Running real-backend Selenium checks in local mode --"
FINANCE_DASHBOARD_URL=local ./hacks/validate-ui-selenium.sh

if [ -x "./hacks/propose-category-rules.sh" ]; then
  if [ -f "/data/finance.db" ] || [ -n "${FINANCE_DB_PATH:-}" ]; then
    echo "-- Building category defaults preview --"
    ./hacks/propose-category-rules.sh --format markdown --top 10 --min-count 2 || true
  else
    echo "-- Skipping category defaults preview (no finance DB found) --"
  fi
fi

echo "Bootstrap complete."
