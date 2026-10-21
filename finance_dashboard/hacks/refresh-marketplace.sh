#!/usr/bin/env bash
set -euo pipefail

REPO="${1:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"
EXPECTED_VERSION="${2:-}"
MAX_ATTEMPTS="${MAX_ATTEMPTS:-5}"
BASE_DELAY_SECONDS="${BASE_DELAY_SECONDS:-1}"
VERSION_MAX_ATTEMPTS="${VERSION_MAX_ATTEMPTS:-8}"
VERSION_BASE_DELAY_SECONDS="${VERSION_BASE_DELAY_SECONDS:-2}"

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
    break
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

if [ -z "$EXPECTED_VERSION" ]; then
  exit 0
fi

extract_versions() {
  python3 -c 'import json, re, sys
text = sys.stdin.read().strip()

def collect_versions(obj, versions):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"version", "latest", "installed", "latest_version", "installed_version", "version_latest"} and isinstance(value, str):
                versions.append(value)
            collect_versions(value, versions)
    elif isinstance(obj, list):
        for item in obj:
            collect_versions(item, versions)

try:
    payload = json.loads(text)
except Exception:
    matches = re.findall(r"(?i)(?:installed_version|latest_version|version_latest|version|latest|installed)[\"=: ]+([0-9]+(?:\.[0-9]+)+)", text)
    for match in matches:
        print(match)
    raise SystemExit(0)

versions = []
collect_versions(payload, versions)
for version in versions:
    print(version)
'
}

attempt=1
while [ "$attempt" -le "$VERSION_MAX_ATTEMPTS" ]; do
  echo "Checking marketplace sync for version $EXPECTED_VERSION (attempt $attempt/$VERSION_MAX_ATTEMPTS)..."
  if output=$(hab marketplace get "$REPO" --json 2>&1); then
    if printf '%s' "$output" | extract_versions | grep -Fxq "$EXPECTED_VERSION"; then
      echo "Marketplace metadata now reports $EXPECTED_VERSION."
      exit 0
    fi
    echo "Marketplace metadata has not caught up yet."
  else
    status=$?
    printf '%s\n' "$output"
    if printf '%s' "$output" | grep -qiE 'auth|unauthenticated|not authenticated'; then
      echo "Auth failure detected while checking version sync; not retrying."
      exit "$status"
    fi
    if [ "$attempt" -ge "$VERSION_MAX_ATTEMPTS" ]; then
      echo "Marketplace sync check failed after $VERSION_MAX_ATTEMPTS attempts."
      exit "$status"
    fi
  fi

  if [ "$attempt" -ge "$VERSION_MAX_ATTEMPTS" ]; then
    echo "Marketplace sync check failed after $VERSION_MAX_ATTEMPTS attempts."
    exit 1
  fi

  delay=$((VERSION_BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
  echo "Waiting ${delay}s for marketplace metadata to sync..."
  sleep "$delay"
  attempt=$((attempt + 1))
done
