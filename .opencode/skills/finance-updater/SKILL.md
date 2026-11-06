---
name: Finance Updater
description: Publish and force-refresh the Home Assistant Finance Dashboard add-on, then update and verify it with no backup.
---

# Finance Dashboard updater workflow

Use this skill when publishing or deploying a Finance Dashboard release, or when the Home Assistant app store has stale version metadata after a Git push.

## Current architecture and prerequisites

- A long-lived access token (LLAT) authenticates to Home Assistant Core. It cannot call the Supervisor `/api/hassio` proxy; do not rotate the token or use `/api/hassio/store/reload` from the OpenCode shell.
- Forced store refresh is handled by the separate optional **Finance Dashboard Updater** add-on. It has `hassio_api: true` with the Supervisor `manager` role; that role is broad. The helper is manual-start, has `dev_mode: false` by default, and is not installed automatically by this repository.
- The user must manually refresh the app store once so the newly published helper appears, then install it from **Settings → Apps**. The first run can use `--enable-dev-mode` to persist `dev_mode: true` for this Home Assistant installation. Do not add Supervisor permissions to the Finance Dashboard add-on itself.
- The helper client uses `HA_URL` and `HA_TOKEN` from the ignored project `.env` / `hacks/.release.env`. Never print or copy credentials, and never put `SUPERVISOR_TOKEN` in those files.

## Release and deploy

1. Run the focused tests and the project certification/release checks. For UI changes, follow the mobile-first UI loop and verify an iPhone-sized viewport.
2. Bump the Finance add-on patch version in `finance_dashboard/config.yaml` before publishing; do not reuse a published version.
3. Publish the tested source to the repository's `main` branch.
4. After the updater helper is installed, run from the `finance_dashboard/` directory:

   ```bash
   ./hacks/force-refresh-deploy.sh --enable-dev-mode
   ```

   Use the flag once for this installation; subsequent runs can omit it. The script starts the helper, passes the expected version from `config.yaml`, and stops the helper afterward.

The helper calls Supervisor `/store/reload`, waits until the exact expected Finance version is advertised, updates only `10d6f312_custom_finance` with `backup: false`, follows the job, and verifies the installed version. It refuses to downgrade or update if the store still advertises an older version. A timeout is uncertain; inspect Supervisor jobs and the installed version before retrying. Do not blindly repeat an update.

The updater prints a bounded result without credentials or Finance transaction data. Keep the helper stopped except while running the script. To disable it for this installation, turn `dev_mode` off in the helper's Configuration tab.
