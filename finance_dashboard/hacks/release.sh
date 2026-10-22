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

export GH_TOKEN="${GH_TOKEN:-}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-}"

# Phase 1: certify locally and publish the source. Home Assistant deployment is
# deliberately handled separately through the finance-dashboard-deploy skill/MCP.
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
  "$ROOT_DIR/hacks/push-github.sh" origin main
else
  echo "No changes to commit after certification."
fi

echo "Local certification and GitHub publish phase complete for finance add-on v$VERSION."
echo "Use the finance-dashboard-deploy OpenCode skill to check the Supervisor store and deploy through Home Assistant MCP."
