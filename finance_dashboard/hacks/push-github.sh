#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"

if [ -z "$TOKEN" ] && command -v gh >/dev/null 2>&1; then
  TOKEN="$(gh auth token 2>/dev/null || true)"
fi

# OpenCode's managed GitHub credential store is a final non-interactive
# fallback. The token is captured only in a shell variable and is never printed.
AUTH_FILE="${OPENCODE_GITHUB_AUTH_FILE:-/data/.config/openchamber/github-auth.json}"
if [ -z "$TOKEN" ] && [ -r "$AUTH_FILE" ]; then
  TOKEN="$(python3 - "$AUTH_FILE" <<'PY'
import json
import sys
try:
    payload = json.load(open(sys.argv[1]))
    credentials = payload.get("credentials", [])
    for credential in credentials:
        token = credential.get("accessToken")
        if token:
            print(token)
            raise SystemExit(0)
except Exception:
    pass
raise SystemExit(1)
PY
  )" || TOKEN=""
fi

if [ -z "$TOKEN" ]; then
  exec git -C "$ROOT_DIR" push "$@"
fi

ASKPASS="$(mktemp /data/v2/cache/opencode/finance-git-askpass.XXXXXX)"
trap 'rm -f "$ASKPASS"' EXIT
cat >"$ASKPASS" <<'SH'
#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *) printf '%s\n' "$GITHUB_TOKEN" ;;
esac
SH
chmod 700 "$ASKPASS"
GITHUB_TOKEN="$TOKEN" GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 git -C "$ROOT_DIR" push "$@"
