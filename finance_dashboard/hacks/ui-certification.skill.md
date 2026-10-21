# UI Certification Skill

Use this workflow for any UI/dashboard/frontend/visual change in this repository.

## Required loop
1. Make the UI change.
2. Bump `finance_dashboard/config.yaml` version.
3. Push to GitHub.
4. Refresh Home Assistant marketplace metadata if needed.
5. Update the Home Assistant add-on **without backup** for this project.
6. Validate the running UI in a browser.
7. Capture a screenshot.
8. Retry transient failures with backoff.
9. Do not declare the change complete until the browser validation passes.

## Notes
- For this add-on, updates should use `backup: false` unless explicitly requested otherwise.
- If the browser validation fails, inspect console/runtime errors before retrying.
- Keep the loop tight: version bump, push, deployment verification, then screenshot validation.
