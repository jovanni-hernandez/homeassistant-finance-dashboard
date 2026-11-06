# Finance Dashboard Updater add-on

This optional, manual-start add-on exists only to refresh the Supervisor app store and update the Personal Finance Dashboard. It is separate from the finance app so the finance app itself does not need Supervisor manager access.

## Privileges and opt-in behavior

The updater uses `hassio_api: true` with the `manager` role because Supervisor's `/store/reload` and app update endpoints require it. The manager role is broad: while this helper is running, its Supervisor token can inspect and manage apps/store resources. Install it only if you accept that privilege. `boot: manual` keeps it stopped by default; `dev_mode` also defaults to `false` and the command refuses to run unless enabled in this add-on's configuration. The option is an operational guard, not a substitute for the Supervisor role boundary.

For this Home Assistant installation, install **Finance Dashboard Updater** from the Personal Finance Add-ons repository. Run `hacks/force-refresh-deploy.sh --enable-dev-mode` once to persist `dev_mode: true` for this installation. Leave it stopped; the script starts it for each operation and stops it afterward. To disable the option later, turn it off in the updater add-on's Configuration tab.

## Operation

The helper accepts only `refresh_and_update` over Supervisor stdin. It reloads the app store, checks that the exact version in the Finance Dashboard `config.yaml` is available, updates only `10d6f312_custom_finance` with `backup: false`, follows the job, and verifies the installed version. It refuses stale or newer store versions rather than downgrading. Results are returned on a request-specific Home Assistant event. No SQL or finance data is handled by this helper.
