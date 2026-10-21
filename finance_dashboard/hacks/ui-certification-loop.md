# UI certification loop

Use this loop for any UI/dashboard/frontend/visual change.

## Steps
1. Make the UI change.
2. Bump `finance_dashboard/config.yaml` version.
3. Push to GitHub.
4. Refresh the Home Assistant marketplace/add-on metadata if available.
5. Validate in a browser.
6. Capture a screenshot.
7. Retry transient failures with backoff.
8. Only mark the work complete after browser validation passes.

## Available helpers
- `hacks/bump-version.py`
- `hacks/validate-ui.mjs`
- `hacks/certify.sh`
- `hacks/refresh-marketplace.sh`
- `hacks/deploy-and-verify.sh`
- `hacks/release.sh`

## Notes
- The helper scripts are intentionally repo-local so they can be reused outside OpenCode.
- For this add-on, deployment verification should use `backup: false` unless backup is explicitly requested.
- The refresh helper retries until marketplace metadata reports the expected version, because HA often lags for a few seconds after a push.
- If the browser validation fails, inspect the console errors first.
