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
  echo "HA_URL and HA_TOKEN are required to refresh the Home Assistant app store." >&2
  echo "Set them in finance_dashboard/.env or finance_dashboard/hacks/.release.env." >&2
  exit 1
fi

# Uses Home Assistant's authenticated Supervisor API proxy; no local hab login
# or shell-side Supervisor token is required.
HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" python3 "$ROOT_DIR/hacks/ha_release.py" refresh "$REPO"
