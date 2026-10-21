#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"
ROOT_ENV_FILE="$ROOT_DIR/.env"
ENV_FILE="$ROOT_DIR/hacks/.release.env"

if [ -f "$ROOT_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ROOT_ENV_FILE"
fi

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [ -z "${HA_URL:-}" ] || [ -z "${HA_TOKEN:-}" ]; then
  echo "HA_URL and HA_TOKEN are required for the full release flow."
  echo "Set them in finance_dashboard/.env or finance_dashboard/hacks/.release.env, then rerun."
  exit 1
fi

# Run the local real-backend preflight before publishing; the live deployed-page
# Selenium check runs after deploy-and-verify updates the add-on.
FINANCE_DASHBOARD_URL=local "$ROOT_DIR/hacks/certify.sh"

VERSION="$(python3 - "$ROOT_DIR/config.yaml" <<'PY'
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

git -C "$ROOT_DIR" add -A

if ! git -C "$ROOT_DIR" diff --cached --quiet; then
  git -C "$ROOT_DIR" commit -m "Release v$VERSION"
  git -C "$ROOT_DIR" push
else
  echo "No changes to commit after certification."
fi

"$ROOT_DIR/hacks/deploy-and-verify.sh" "$REPO"

echo "Running Selenium against the deployed finance add-on over the HA internal network..."
"$ROOT_DIR/hacks/validate-ui-selenium.sh"
