# UI Certification Skill

Use this workflow for any UI/dashboard/frontend/visual change in this repository.

## Required loop
1. Make the UI change.
2. Bump `finance_dashboard/config.yaml` version.
3. Push to GitHub.
4. Refresh Home Assistant marketplace metadata if needed.
5. Update the Home Assistant add-on **without backup** for this project.
6. Validate the running UI in a browser.
7. Capture the deployed finance app with Home Assistant's `homeassistant_screenshot_url` MCP tool.
8. Retry transient failures with backoff.
9. Do not declare the change complete until the browser validation passes.

## Notes
- For this add-on, updates should use `backup: false` unless explicitly requested otherwise.
- If the browser validation fails, inspect console/runtime errors before retrying.
- Keep the loop tight: version bump, push, deployment verification, then screenshot validation.
- The deployed app is at the Home Assistant page path `/10d6f312_custom_finance` by default. Pass this relative path only to `homeassistant_screenshot_url` (for example, `url_path: "/10d6f312_custom_finance"`, `width: 390`, `height: 844`). Do not pass an absolute URL; the screenshot tool uses its configured Home Assistant base URL.
- `hacks/certify.sh` cannot call MCP tools directly; after Selenium validation it prints the screenshot path and MCP capture parameters. Use `hacks/certify.sh --screenshot-instructions` to verify the handoff without running release checks or changing the version. Selenium screenshots are test artifacts; the MCP screenshot is the deployed visual capture and is attached to the tool result, not saved to disk.
- For ad hoc deployed browser actions, use `hacks/finance-browser.sh --actions-file <json>`; it authenticates the frontend with the Core LLAT in `.env` and opens the Finance panel through normal Ingress. Read-only `wait`, `count`, and screenshot actions need no flag; clicks, fills, selections, and keypresses require `--apply`. It allows only CSS-selector actions (no arbitrary JavaScript), limits each run to 20 actions, and does not print page text or entered values.
- The full Selenium suite exercises write flows and should only run against the isolated local database unless live test writes are explicitly approved. Its legacy deployed-Ingress bootstrap calls the Core `/api/hassio` proxy, which this HA instance rejects with 401 even when the LLAT authenticates to Core. Use the MCP screenshot tool for deployed visual verification; don't keep rotating the LLAT to fix the Supervisor proxy.
