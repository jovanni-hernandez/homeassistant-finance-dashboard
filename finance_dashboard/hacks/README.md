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
6. runs a Selenium-backed browser validation pass against the critical dashboard sections and recurring-payments view
7. rejects blank/near-blank screenshots with a visual-content check

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
It requires `HA_URL` and `HA_TOKEN`; with those set, it also triggers the Home Assistant add-on update and polls it to completion with `backup: false`.

## Dev launcher add-on

The repo also includes a separate dev-launcher add-on under `finance_dashboard_dev/`. It clones the finance dashboard repo into `/data`, installs the runtime plus Selenium/browser tooling, and boots the app from that workspace.

If you only want the repository refresh + sync wait step:

```bash
./hacks/deploy-and-verify.sh
```

This runs the repo refresh helper using the version from `config.yaml`, then waits for the marketplace metadata to report that version before it triggers the Home Assistant update step.
If you set `HA_URL` and `HA_TOKEN`, it will also drive the add-on update end-to-end, auto-detect the update entity, retry transient update-trigger failures, and wait for Home Assistant to report the new installed version.

## Categorization rule proposals

```bash
./hacks/propose-category-rules.sh --format markdown
```

Reads the encrypted finance database through the app models and prints merchant/category proposals plus uncategorized items to review.
Use `--format json` if you want machine-readable output.

You can also build the same defaults from the dashboard Settings panel with the "Build defaults" button.

## AI-assisted categorization

The dashboard also supports AI-generated defaults through the Settings panel.
Configure the provider, model, and keys under **AI assistance**, then use **Build with AI** in Category rules.

You can also store those values in `hacks/.release.env` (copy from `hacks/release.env.example`) so the release command works without exporting env vars every time.
The helpers also read `finance_dashboard/.env` if you prefer one repo-root env file for all release settings.
