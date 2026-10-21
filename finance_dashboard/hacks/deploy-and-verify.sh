#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# This script handles the repo-side release prep only.
# Home Assistant deployment must be performed with backup disabled for this add-on:
#   homeassistant_update_component(component='addon', addon_slug='10d6f312_custom_finance', backup=False)

echo "Repo release prep complete."
echo "Next step in Home Assistant: update the add-on with backup disabled, then validate the running UI."
