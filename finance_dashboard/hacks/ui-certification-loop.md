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
- `hacks/test-ha-release.py`
- `hacks/validate-ui-selenium.sh`
- `hacks/validate-ui-real.py`
- `hacks/certify.sh`
- `hacks/refresh-marketplace.sh`
- `hacks/deploy-and-verify.sh`
- `hacks/release.sh`

## Notes
- The helper scripts are intentionally repo-local so they can be reused outside OpenCode.
- `validate-ui-selenium.sh` defaults to the deployed add-on through an authenticated HA Ingress session using `HA_URL`/`HA_TOKEN`. Use `FINANCE_DASHBOARD_URL=local` only for a pre-publish real-backend check; it does not satisfy deployed acceptance.
- For this add-on, deployment verification should use `backup: false` unless backup is explicitly requested.
- The refresh helper retries until marketplace metadata reports the expected version, because HA often lags for a few seconds after a push.
- If the browser validation fails, inspect the console errors first.
