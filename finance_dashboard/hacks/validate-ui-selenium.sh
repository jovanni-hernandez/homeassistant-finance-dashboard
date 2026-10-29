#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
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

ARTIFACT_DIR="${FINANCE_TEST_ARTIFACT_DIR:-}"
if [ -z "$ARTIFACT_DIR" ]; then
  if mkdir -p /data/v2/cache/opencode 2>/dev/null && [ -w /data/v2/cache/opencode ]; then
    ARTIFACT_DIR="/data/v2/cache/opencode"
  else
    ARTIFACT_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/opencode"
  fi
fi
mkdir -p "$ARTIFACT_DIR"
export FINANCE_TEST_ARTIFACT_DIR="$ARTIFACT_DIR"
VENV_DIR="${FINANCE_TEST_VENV:-$ARTIFACT_DIR/finance-dashboard-selenium-venv}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install selenium pillow fastapi uvicorn requests sqlalchemy cryptography >/dev/null
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-recurring-analysis.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-api-validation.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-admin-stdin.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-simplefin-sync.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-account-types.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-manual-assets.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-manual-category-rule.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-transaction-pagination.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-goals.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-ai-model-discovery.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-ai-prompts.py"
"$VENV_DIR/bin/python" "$ROOT_DIR/hacks/test-ai-chat-privacy.py"

MODE_ARGS=()
if [ "${FINANCE_DASHBOARD_URL:-}" = "local" ]; then
  :
elif [ -n "${FINANCE_DASHBOARD_URL:-}" ]; then
  MODE_ARGS=(--url "$FINANCE_DASHBOARD_URL")
elif [ -n "${HA_URL:-}" ] && [ -n "${HA_TOKEN:-}" ]; then
  MODE_ARGS=(--ha-ingress)
else
  echo "Live Selenium requires HA_URL and HA_TOKEN for an authenticated Ingress session." >&2
  echo "Set them in finance_dashboard/.env or finance_dashboard/hacks/.release.env." >&2
  echo "Use FINANCE_DASHBOARD_URL=local only for the local preflight." >&2
  exit 1
fi

for device in desktop iphone pixel; do
  echo "Selenium deployed-UI check: $device viewport"
  "$VENV_DIR/bin/python" "$ROOT_DIR/hacks/validate-ui-real.py" \
    --file "$ROOT_DIR/frontend/index.html" "${MODE_ARGS[@]}" --device "$device" "$@"
done
