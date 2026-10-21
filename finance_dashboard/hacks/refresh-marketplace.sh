#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"

if ! command -v hab >/dev/null 2>&1; then
  echo "hab CLI not available in this shell."
  echo "Equivalent Home Assistant command: hab marketplace refresh $REPO"
  echo "Run that inside an OpenCode / hab-enabled session."
  exit 1
fi

if ! hab auth status --json | jq -e '.data.authenticated == true' >/dev/null 2>&1; then
  echo "hab is installed but not authenticated in this shell."
  echo "Run: hab auth login"
  echo "Then: hab marketplace refresh $REPO"
  exit 1
fi

hab marketplace refresh "$REPO"
