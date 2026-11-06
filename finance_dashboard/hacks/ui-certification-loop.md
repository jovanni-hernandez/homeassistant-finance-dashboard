# UI certification loop

Use this loop for any UI/dashboard/frontend/visual change.

## Steps
1. Make the UI change.
2. Bump `finance_dashboard/config.yaml` version.
3. Push to GitHub.
4. Refresh the Home Assistant marketplace/add-on metadata if available.
5. Validate in a browser.
6. Capture the deployed app with Home Assistant's `homeassistant_screenshot_url` MCP tool.
7. Retry transient failures with backoff.
8. Only mark the work complete after browser validation passes.

## Available helpers
- `hacks/bump-version.py`
- `hacks/test-ha-release.py`
- `hacks/validate-ui-selenium.sh`
- `hacks/validate-ui-real.py`
- `hacks/certify.sh`
- `hacks/refresh-marketplace.sh`
- `hacks/deploy-and-verify.sh`
- `hacks/release.sh`

## Notes
- The helper scripts are intentionally repo-local so they can be reused outside OpenCode.
- `validate-ui-selenium.sh`'s legacy deployed-Ingress bootstrap uses the Core `/api/hassio` proxy, which this HA instance rejects with 401; a valid Core LLAT does not fix it. `FINANCE_DASHBOARD_URL=local` runs isolated tests only. Use the MCP screenshot tool for deployed visual verification.
- The MCP screenshot path defaults to `/10d6f312_custom_finance`. Pass this relative path (not the full URL) to `homeassistant_screenshot_url` at `390 × 844` for the phone-first check; the image is attached to the tool result rather than saved to disk.
- `hacks/certify.sh` prints the MCP screenshot parameters after Selenium checks. Run `hacks/certify.sh --screenshot-instructions` to test that handoff without version bump or release checks. Override the path with `FINANCE_DASHBOARD_SCREENSHOT_PATH` if Ingress changes.
- The full Selenium suite performs write actions and is for the isolated local database unless live writes are explicitly approved.
- For this add-on, deployment verification should use `backup: false` unless backup is explicitly requested.
- The refresh helper retries until marketplace metadata reports the expected version, because HA often lags for a few seconds after a push.
- If the browser validation fails, inspect the console errors first.
