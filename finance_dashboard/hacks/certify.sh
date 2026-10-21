#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/config.yaml"
FRONTEND="$ROOT_DIR/frontend/index.html"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-2}"

python3 "$ROOT_DIR/hacks/bump-version.py" "$VERSION_FILE" patch >/dev/null
python3 -m py_compile "$ROOT_DIR/backend"/*.py

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  echo "Real-app Selenium validation attempt $attempt/$MAX_ATTEMPTS..."
  if "$ROOT_DIR/hacks/validate-ui-selenium.sh" --file "$FRONTEND"; then
    break
  else
    status=$?
  fi

  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "Real-app Selenium validation failed after $MAX_ATTEMPTS attempts."
    exit "$status"
  fi

  delay=$((BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
  echo "Transient UI validation failure; retrying in ${delay}s..."
  sleep "$delay"
  attempt=$((attempt + 1))
done

git -C "$ROOT_DIR" status --short
