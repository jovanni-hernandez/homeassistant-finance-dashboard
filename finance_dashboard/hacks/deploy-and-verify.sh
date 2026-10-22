#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_ENV_FILE="$ROOT_DIR/.env"
ENV_FILE="$ROOT_DIR/hacks/.release.env"
REPO="${1:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"

if [ -f "$ROOT_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ROOT_ENV_FILE"
fi
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

if [ -z "${HA_URL:-}" ] || [ -z "${HA_TOKEN:-}" ]; then
  echo "HA_URL and HA_TOKEN are required for deployment verification." >&2
  echo "Set them in finance_dashboard/.env or finance_dashboard/hacks/.release.env." >&2
  exit 1
fi

HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" python3 "$ROOT_DIR/hacks/ha_release.py" deploy "$REPO"

echo "Running Selenium against the deployed finance add-on on the HA internal network..."
HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" FINANCE_DASHBOARD_URL="${FINANCE_DASHBOARD_URL:-}" "$ROOT_DIR/hacks/validate-ui-selenium.sh"
