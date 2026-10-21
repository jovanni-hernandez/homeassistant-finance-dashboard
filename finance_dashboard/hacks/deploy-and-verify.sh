#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT_ENV_FILE="$ROOT_DIR/.env"
ENV_FILE="$ROOT_DIR/hacks/.release.env"
REPO="${1:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard}"
VERSION_FILE="$ROOT_DIR/config.yaml"
EXPECTED_VERSION="$(python3 - "$VERSION_FILE" <<'PY'
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

ADDON_TITLE="$(python3 - "$VERSION_FILE" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text()
match = re.search(r'^name:\s*(.+?)\s*$', text, re.M)
if not match:
    raise SystemExit('Could not determine add-on title from config.yaml')
print(match.group(1))
PY
)"

HA_URL="${HA_URL:-}"
HA_TOKEN="${HA_TOKEN:-}"
UPDATE_ENTITY_ID="${UPDATE_ENTITY_ID:-}"
HA_MAX_ATTEMPTS="${HA_MAX_ATTEMPTS:-10}"
HA_BASE_DELAY_SECONDS="${HA_BASE_DELAY_SECONDS:-2}"

if [ -f "$ROOT_ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ROOT_ENV_FILE"
fi

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

"$ROOT_DIR/hacks/refresh-marketplace.sh" "$REPO" "$EXPECTED_VERSION"

echo "Repo release prep complete."
echo "Marketplace metadata now reports $EXPECTED_VERSION."

if [ -z "$HA_URL" ] || [ -z "$HA_TOKEN" ]; then
  echo "HA_URL / HA_TOKEN not set; skipping live Home Assistant update step."
  echo "Next step in Home Assistant: update the add-on with backup disabled, then validate the running UI."
  exit 0
fi

ha_call() {
  local method="$1"
  local path="$2"
  local payload="${3:-}"

  HA_URL="$HA_URL" HA_TOKEN="$HA_TOKEN" python3 - "$method" "$path" "$payload" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

method, path, payload = sys.argv[1:4]
base = os.environ['HA_URL'].rstrip('/')
token = os.environ['HA_TOKEN']

data = None
headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json',
    'Accept': 'application/json',
}

if payload and payload != '__EMPTY__':
    data = payload.encode('utf-8')

req = urllib.request.Request(f'{base}{path}', data=data, headers=headers, method=method)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        sys.stdout.write(resp.read().decode('utf-8'))
except urllib.error.HTTPError as exc:
    body = exc.read().decode('utf-8', errors='replace') if exc.fp else ''
    sys.stderr.write(body or str(exc))
    raise SystemExit(exc.code)
PY
}

parse_state_field() {
  local field="$1"
  python3 - "$field" <<'PY'
import json
import sys

field = sys.argv[1]
text = sys.stdin.read().strip()
if not text:
    raise SystemExit(1)
payload = json.loads(text)
value = payload.get('attributes', {}).get(field)
if value is None and field in payload:
    value = payload[field]
if value is None:
    raise SystemExit(2)
print(str(value).lower())
PY
}

detect_update_entity_id() {
  if [ -n "$UPDATE_ENTITY_ID" ]; then
    return 0
  fi

  UPDATE_ENTITY_ID="$(ha_call GET "/api/states" '__EMPTY__' | python3 - "$ADDON_TITLE" <<'PY'
import json
import re
import sys

target = sys.argv[1].strip().lower()
payload = json.loads(sys.stdin.read() or '[]')

def normalize(text):
    return re.sub(r'\s+', ' ', str(text or '').strip().lower())

for entity in payload:
    if entity.get('entity_id', '').startswith('update.'):
        attrs = entity.get('attributes', {})
        title = normalize(attrs.get('title'))
        friendly = normalize(attrs.get('friendly_name'))
        if title == target or friendly == target:
            print(entity['entity_id'])
            raise SystemExit(0)

raise SystemExit(1)
PY
)"

  if [ -z "$UPDATE_ENTITY_ID" ]; then
    echo "Could not auto-detect the Home Assistant update entity for $ADDON_TITLE."
    echo "Set UPDATE_ENTITY_ID manually and re-run."
    exit 1
  fi
}

refresh_update_entity() {
  ha_call POST "/api/services/homeassistant/update_entity" "$(python3 - <<'PY' "$UPDATE_ENTITY_ID"
import json
import sys
print(json.dumps({"entity_id": sys.argv[1]}))
PY
)" >/dev/null
}

install_update() {
  local attempt=1

  while [ "$attempt" -le "$HA_MAX_ATTEMPTS" ]; do
    echo "Triggering Home Assistant add-on update (attempt $attempt/$HA_MAX_ATTEMPTS)..."

    if output="$(ha_call POST "/api/services/update/install" "$(python3 - <<'PY' "$UPDATE_ENTITY_ID"
import json
import sys
print(json.dumps({"entity_id": sys.argv[1], "backup": False}))
PY
)" 2>&1)"; then
      printf '%s\n' "$output"
      return 0
    fi

    status=$?
    printf '%s\n' "$output"

    if printf '%s' "$output" | grep -qiE 'auth|unauthenticated|not authenticated'; then
      echo "Auth failure detected while triggering update; not retrying."
      exit "$status"
    fi

    if [ "$attempt" -ge "$HA_MAX_ATTEMPTS" ]; then
      echo "Home Assistant update trigger failed after $HA_MAX_ATTEMPTS attempts."
      return "$status"
    fi

    delay=$((HA_BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
    echo "Transient failure; retrying in ${delay}s..."
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}

get_update_state() {
  ha_call GET "/api/states/$UPDATE_ENTITY_ID" '__EMPTY__'
}

wait_for_update_entity() {
  local target_version="$1"
  local phase="$2"
  local attempt=1

  while [ "$attempt" -le "$HA_MAX_ATTEMPTS" ]; do
    echo "Refreshing Home Assistant update entity ($phase, attempt $attempt/$HA_MAX_ATTEMPTS)..."
    if ! refresh_update_entity; then
      echo "Home Assistant refresh call failed; retrying."
      delay=$((HA_BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
      sleep "$delay"
      attempt=$((attempt + 1))
      continue
    fi

    if ! state_json="$(get_update_state)"; then
      echo "Home Assistant update entity state was unavailable; retrying."
      delay=$((HA_BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
      sleep "$delay"
      attempt=$((attempt + 1))
      continue
    fi

    installed_version="$(printf '%s' "$state_json" | parse_state_field installed_version || true)"
    latest_version="$(printf '%s' "$state_json" | parse_state_field latest_version || true)"
    in_progress="$(printf '%s' "$state_json" | parse_state_field in_progress || true)"

    if [ "$installed_version" = "$target_version" ] && [ "$in_progress" = "false" ]; then
      if [ -z "$latest_version" ] || [ "$latest_version" = "$target_version" ]; then
        echo "Home Assistant reports $target_version installed."
        return 0
      fi
    fi

    if [ "$attempt" -ge "$HA_MAX_ATTEMPTS" ]; then
      echo "Home Assistant update step did not settle after $HA_MAX_ATTEMPTS attempts."
      echo "Latest observed state: installed=$installed_version latest=$latest_version in_progress=$in_progress"
      return 1
    fi

    delay=$((HA_BASE_DELAY_SECONDS * (2 ** (attempt - 1))))
    echo "Waiting ${delay}s for Home Assistant to finish update step..."
    sleep "$delay"
    attempt=$((attempt + 1))
  done
}


detect_update_entity_id
echo "Using Home Assistant update entity: $UPDATE_ENTITY_ID"

current_state_json="$(get_update_state || true)"
current_installed_version="$(printf '%s' "$current_state_json" | parse_state_field installed_version || true)"
current_latest_version="$(printf '%s' "$current_state_json" | parse_state_field latest_version || true)"

if [ "$current_installed_version" != "$EXPECTED_VERSION" ] || [ "$current_latest_version" != "$EXPECTED_VERSION" ]; then
  echo "Triggering Home Assistant add-on update with backup disabled..."
  install_update
  wait_for_update_entity "$EXPECTED_VERSION" "post-install"
else
  echo "Home Assistant already reports the expected version; skipping update call."
fi

echo "Live Home Assistant verification complete."
