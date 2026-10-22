#!/usr/bin/env bash
set -euo pipefail

WORKDIR="${FINANCE_DEV_WORKDIR:-/data/finance-dashboard-workspace}"
JOBS_DIR="${FINANCE_DEV_JOBS:-/data/finance-dashboard-jobs}"
TEST_CACHE="${FINANCE_TEST_ARTIFACT_DIR:-/data/v2/cache/opencode}"
TEST_VENV="${FINANCE_TEST_VENV:-$TEST_CACHE/finance-dashboard-selenium-venv}"
mkdir -p "$WORKDIR" "$JOBS_DIR" "$TEST_CACHE" "$TEST_VENV"
chown -R vscode:vscode "$WORKDIR" "$JOBS_DIR" "$TEST_VENV"
chown vscode:vscode "$TEST_CACHE"
export FINANCE_TEST_ARTIFACT_DIR="$TEST_CACHE"
export FINANCE_TEST_VENV="$TEST_VENV"

exec su -p -s /bin/bash vscode -c 'exec python3 /app/launcher.py'
