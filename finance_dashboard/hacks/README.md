# Hacks / certification helpers

## UI certification loop

Run from the repository root:

```bash
./hacks/certify.sh
```

What it does:
1. bumps `config.yaml` patch version
2. tests the Supervisor refresh/update job protocol
3. syntax-checks backend Python
4. runs Selenium against the deployed app by default (or the real FastAPI app with an isolated DB when explicitly set to `FINANCE_DASHBOARD_URL=local`)
5. checks the critical dashboard sections, recurring view, and quick filters
6. rejects blank/near-blank screenshots
7. fails if Vue renders warnings, browser requests fail, or the boot fallback remains visible
8. retries transient browser failures with exponential backoff

The Selenium helper defaults to the deployed app at `http://homeassistant:8000/` over Home Assistant's internal network. Set `FINANCE_DASHBOARD_URL` to another directly reachable app URL to override it. `FINANCE_DASHBOARD_URL=local` runs the real FastAPI app with a disposable SQLite database as a pre-publish check; it is not the deployed-app acceptance test.

## Marketplace refresh

```bash
./hacks/refresh-marketplace.sh
```

This calls the authenticated Supervisor store-reload endpoint through HA's REST proxy, then retries the store-version check until `config.yaml`'s version appears. It requires `HA_URL` and `HA_TOKEN` from `.env` or `hacks/.release.env`; it does not depend on a shell `hab` login.

## Release / verify helper

```bash
./hacks/release.sh
```

This runs the full release flow: local real-backend preflight, authenticated GitHub push, Supervisor store refresh through `/api/hassio/store/reload`, add-on update with `backup: false`, job/version verification, and finally Selenium against the deployed add-on over the HA internal network.
It requires `HA_URL` and an administrator `HA_TOKEN` in `.env` or `hacks/.release.env`. Git push uses existing Git/`gh` auth, `GITHUB_TOKEN`/`GH_TOKEN`, or OpenCode's managed GitHub credential store. The Selenium acceptance target defaults to `http://homeassistant:8000/`; set `FINANCE_DASHBOARD_URL` to override it.

`deploy-and-verify.sh` can be run by itself to refresh the store, update the currently published add-on, confirm its installed version, and run the live Selenium check. `refresh-marketplace.sh` performs just the authenticated store refresh/version wait.

## Dev launcher add-on

The repo also includes a separate dev-launcher add-on under `finance_dashboard_dev/`. It clones the finance dashboard repo into `/data`, installs the runtime plus Selenium/browser tooling, and boots the launcher UI from that workspace.

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
