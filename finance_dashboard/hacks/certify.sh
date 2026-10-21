#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/config.yaml"
FRONTEND="$ROOT_DIR/frontend/index.html"
SCREENSHOT="/data/v2/cache/opencode/finance-dashboard-certify.png"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-2}"

python3 "$ROOT_DIR/hacks/bump-version.py" "$VERSION_FILE" patch >/dev/null
python3 -m py_compile "$ROOT_DIR/backend"/*.py

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  echo "UI validation attempt $attempt/$MAX_ATTEMPTS..."
  if node "$ROOT_DIR/hacks/validate-ui.mjs" --file "$FRONTEND" --screenshot "$SCREENSHOT"; then
    break
  fi

  status=$?
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "UI validation failed after $MAX_ATTEMPTS attempts."
    exit "$status"
  fi

  delay=$((BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
  echo "Transient UI validation failure; retrying in ${delay}s..."
  sleep "$delay"
  attempt=$((attempt + 1))
done

echo "Running Selenium UI validation..."
"$ROOT_DIR/hacks/validate-ui-selenium.sh" --file "$FRONTEND" --screenshot "${SCREENSHOT%.png}-selenium.png"

git -C "$ROOT_DIR" status --short
