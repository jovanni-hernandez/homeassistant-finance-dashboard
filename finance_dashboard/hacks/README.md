# Hacks / certification helpers

## UI certification loop

Run from the repository root:

```bash
./hacks/certify.sh
```

What it does:
1. bumps `config.yaml` patch version
2. syntax-checks backend Python
3. opens the frontend in headless Chromium
4. captures a screenshot and checks for runtime exceptions
5. retries transient browser failures with exponential backoff

## Manual HA refresh

Home Assistant's add-on store refresh is the equivalent of a repo metadata check. The underlying HA tooling exposes a `hab marketplace refresh <repository>` command, but the bridge can be flaky in this environment. Use the HA UI refresh button when needed, or run the matching `hab` command if available in your session.


## Repository refresh

```bash
./hacks/refresh-marketplace.sh
```

This mirrors the Home Assistant marketplace refresh button and retries transient failures with exponential backoff. If you pass an expected version, it also retries the post-refresh version check until the metadata catches up.

## Release / verify helper

```bash
./hacks/release.sh
```

This runs the full release flow: certify, commit/push, refresh marketplace metadata, and wait for the version sync.
If `HA_URL` and `HA_TOKEN` are set, it also triggers the Home Assistant add-on update and polls it to completion with `backup: false`.

If you only want the repository refresh + sync wait step:

```bash
./hacks/deploy-and-verify.sh
```

This runs the repo refresh helper using the version from `config.yaml`, then waits for the marketplace metadata to report that version before you move on to the Home Assistant update step.
If you set `HA_URL` and `HA_TOKEN`, it will also drive the add-on update end-to-end, auto-detect the update entity, and wait for Home Assistant to report the new installed version.
