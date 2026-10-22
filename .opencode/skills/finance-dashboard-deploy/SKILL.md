---
name: Finance Dashboard Deploy
description: Release the finance dashboard, then deploy and verify its Home Assistant add-on through MCP.
---

# Finance Dashboard release and deployment

Use this workflow only when the user asks to release or deploy the Finance Dashboard. A skill is guidance, not blanket authorization: get explicit approval before committing/pushing release changes or starting the Home Assistant add-on update unless the user's current request clearly authorizes those actions.

## Phase 1: local certification and GitHub publish

1. Read `finance_dashboard/config.yaml` and the repository status. Do not read or print `.env`, `hacks/.release.env`, or any other credential file.
2. Review the intended changes and confirm the target version. `finance_dashboard/hacks/release.sh` runs the version bump, local certification (including the real-backend desktop/iPhone/Pixel Selenium checks), creates the release commit, and pushes `main`. It does not need `HA_URL` or `HA_TOKEN` and does not deploy to Home Assistant.
3. Run the release script only after the user has authorized publishing. If certification, commit, or push fails, stop and report the failure; do not continue to deployment.
4. Read back the version from `finance_dashboard/config.yaml` and the Git status/log after the push. Do not claim the release is published if the push result is uncertain.

## Phase 2: Home Assistant MCP deployment

The finance add-on slug is `10d6f312_custom_finance`.

1. Read the expected version from `finance_dashboard/config.yaml`.
2. Probe for the new release immediately; do not wait for the periodic update scan. Use up to six bounded probe attempts. On each attempt:
   - Call `homeassistant_get_available_updates` with `component: "addons"` and inspect the exact finance add-on entry.
   - Force Home Assistant Core to poll the add-on update entity with `homeassistant_call_service(domain: "homeassistant", service: "update_entity", target: {entity_id: "update.personal_finance_dashboard_update"})`, then read it using `homeassistant_get_entity_details`.
   - Continue as soon as the reported latest version matches the expected version and an update is available (or it is already installed at that version).
   - If metadata is still behind, wait before the next attempt using bounded pauses of 2, 4, 8, 8, and 8 seconds. Use the shell only for these short waits; total wait is at most 30 seconds. Do not run the update while either Supervisor or the update entity still reports a different latest version.
3. If all six attempts remain stale, stop and report the installed/latest versions observed. The entity poll refreshes Core's Supervisor coordinator; it does not itself pull the Git repository into Supervisor's app store. The existing release helper forces that store pull with `POST /api/hassio/store/reload`, but this MCP profile does not expose that endpoint or an equivalent store-reload service. Do not invent a service name or bypass MCP credentials; ask the user to trigger the store reload in Home Assistant, then run the bounded probe loop again.
4. Use `homeassistant_get_store_audit` as an additional repository check if the version report is ambiguous; do not infer update availability from another add-on or from stale prior-session data.
5. Before a new update, confirm the user authorized the deployment. Call `homeassistant_update_component` with `component: "addon"`, `addon_slug: "10d6f312_custom_finance"`, and `backup: false` (unless the user explicitly requests a backup).
6. If the update returns a job ID, monitor it with `homeassistant_get_update_progress`. Do not blindly retry a failed or uncertain update. Re-read `homeassistant_get_available_updates` after completion and verify the installed version equals the expected version.
7. Report local certification, GitHub publish, Supervisor update, and version verification as separate results. MCP update/version verification does not prove the Ingress UI rendered correctly. The authenticated deployed-page Selenium check still requires the existing credential-backed path; do not claim it passed unless it was actually run.

## Safety

- Never display, copy into notes, or log Home Assistant or GitHub credentials.
- Do not update when the store's latest version differs from the repository's expected version.
- Do not run unrelated add-on updates as part of this workflow.
- If any operation times out, inspect live state before retrying.
