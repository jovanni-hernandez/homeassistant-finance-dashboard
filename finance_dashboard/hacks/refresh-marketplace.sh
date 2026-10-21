#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-1}"

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

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  echo "Refreshing marketplace repository (attempt $attempt/$MAX_ATTEMPTS)..."
  if output=$(hab marketplace refresh "$REPO" 2>&1); then
    printf '%s\n' "$output"
    echo "Marketplace refresh succeeded."
    exit 0
  fi

  status=$?
  printf '%s\n' "$output"

  if printf '%s' "$output" | grep -qiE 'auth|unauthenticated|not authenticated'; then
    echo "Auth failure detected; not retrying."
    exit "$status"
  fi

  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "Marketplace refresh failed after $MAX_ATTEMPTS attempts."
    exit "$status"
  fi

  delay=$((BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
  echo "Transient failure; retrying in ${delay}s..."
  sleep "$delay"
  attempt=$((attempt + 1))
done
