#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VERSION_FILE="$ROOT_DIR/config.yaml"
FRONTEND="$ROOT_DIR/frontend/index.html"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-2}"
SCREENSHOT_PATH="${FINANCE_DASHBOARD_SCREENSHOT_PATH:-/10d6f312_custom_finance}"

if [[ "$SCREENSHOT_PATH" != /* || "$SCREENSHOT_PATH" == //* || "$SCREENSHOT_PATH" == *://* ]]; then
  echo "FINANCE_DASHBOARD_SCREENSHOT_PATH must be a Home Assistant page path, e.g. /10d6f312_custom_finance" >&2
  exit 2
fi

print_screenshot_instructions() {
  echo "Capture deployed UI with homeassistant_screenshot_url using:"
  echo "  url_path: \"$SCREENSHOT_PATH\""
  echo "  width: 390"
  echo "  height: 844"
  echo "Pass the relative path only; the screenshot tool uses its configured Home Assistant base URL."
}

if [ "${1:-}" = "--screenshot-instructions" ]; then
  print_screenshot_instructions
  exit 0
fi

python3 "$ROOT_DIR/hacks/bump-version.py" "$VERSION_FILE" patch >/dev/null
python3 "$ROOT_DIR/hacks/test-ha-release.py"
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

print_screenshot_instructions

git -C "$ROOT_DIR" status --short
