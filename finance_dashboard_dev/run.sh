#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${FINANCE_DEV_REPO_URL:-https://github.com/jovanni-hernandez/homeassistant-finance-dashboard.git}"
BRANCH="${FINANCE_DEV_BRANCH:-main}"
WORKDIR="${FINANCE_DEV_WORKDIR:-/data/finance-dashboard-workspace}"
VENV_DIR="${FINANCE_DEV_VENV:-/data/finance-dashboard-venv}"

mkdir -p "$WORKDIR"

if [ ! -d "$WORKDIR/.git" ]; then
  echo "Cloning finance dashboard workspace into $WORKDIR"
  git clone --branch "$BRANCH" --single-branch "$REPO_URL" "$WORKDIR"
else
  echo "Updating finance dashboard workspace in $WORKDIR"
  git -C "$WORKDIR" pull --ff-only origin "$BRANCH"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$WORKDIR/backend/requirements.txt" selenium >/dev/null

cd "$WORKDIR/backend"
exec "$VENV_DIR/bin/python" -m uvicorn main:app --host 0.0.0.0 --port 8000
