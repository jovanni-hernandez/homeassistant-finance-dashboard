#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

echo "== Finance Dashboard bootstrap =="
echo "Python: $(python --version)"
echo "Node: $(node --version)"

echo "-- Checking backend Python syntax --"
python -m py_compile backend/*.py

echo "-- Checking frontend JavaScript syntax --"
python3 - <<'PY'
from pathlib import Path
import re

text = Path('frontend/index.html').read_text()
script = re.search(r'<script>(.*)</script>\s*</body>', text, re.S)
if not script:
    raise SystemExit('script block not found')

out = Path('.devcontainer') / 'frontend-check.js'
out.write_text(script.group(1))
PY
node --check .devcontainer/frontend-check.js

if [ -x "./hacks/propose-category-rules.sh" ]; then
  if [ -f "/data/finance.db" ] || [ -n "${FINANCE_DB_PATH:-}" ]; then
    echo "-- Building category defaults preview --"
    ./hacks/propose-category-rules.sh --format markdown --top 10 --min-count 2 || true
  else
    echo "-- Skipping category defaults preview (no finance DB found) --"
  fi
fi

echo "Bootstrap complete."
