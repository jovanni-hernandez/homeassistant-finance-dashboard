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

`FINANCE_DASHBOARD_URL=local` runs the real FastAPI app with a disposable SQLite database. The deployed Selenium helper's legacy bootstrap calls Home Assistant's `/api/hassio` proxy, which this instance rejects with 401; a valid owner LLAT cannot fix that. Use `homeassistant_screenshot_url` for deployed visual verification.

## Marketplace refresh

```bash
./hacks/refresh-marketplace.sh
```

This legacy release helper calls HA's `/api/hassio` Core proxy and is blocked on this instance by Supervisor API authorization. Do not rotate `HA_TOKEN` to address that; it authenticates to Core, while the proxy rejects Supervisor access.

## Release / verify helper

```bash
./hacks/release.sh
```

This runs the full release flow: local real-backend preflight, authenticated GitHub push, Supervisor store refresh through `/api/hassio/store/reload`, add-on update with `backup: false`, job/version verification, and finally Selenium through authenticated HA Ingress at desktop, iPhone, and Pixel viewports.
It requires `HA_URL` and an administrator `HA_TOKEN` in `.env` or `hacks/.release.env`. Git push uses existing Git/`gh` auth, `GITHUB_TOKEN`/`GH_TOKEN`, or OpenCode's managed GitHub credential store. The add-on is ingress-only; its host port is not published.

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

## Deployed Finance SQL

```bash
./hacks/finance-sql.sh "SELECT id, date, finance_decrypt(payee), finance_decrypt(amount), category FROM transactions LIMIT 50"
```

This sends one SQL statement through Home Assistant's `hassio.app_stdin` service to the Finance add-on; it does not expose the SQLite file or use `/api/hassio`. Results return on a request-specific Home Assistant event and print as JSON. The add-on needs `homeassistant_api: true` to publish that event. The CLI reads `HA_URL`, `HA_TOKEN`, and optional `FINANCE_SESSION_TOKEN` from the ignored root `.env` (or `hacks/.release.env`); keep them private.

Queries are read-only by default. Writes require both `--write` and `--apply`. Each call accepts one statement, blocks `ATTACH`/`DETACH`, extension loading and explicit transaction control, and caps responses at 200 rows / 256 KiB. Use `finance_encrypt(value)` / `finance_decrypt(column)` for encrypted finance columns; reads of encrypted `settings.value` are blocked to protect SimpleFIN and provider credentials. Write statements can permanently alter or delete data; back up first. A timeout leaves the outcome unknown—inspect before retrying.

## AI-assisted categorization

The dashboard also supports AI-generated defaults through the Settings panel.
Configure the provider, model, and keys under **AI assistance**, then use **Build with AI** in Category rules.

You can also store those values in `hacks/.release.env` (copy from `hacks/release.env.example`) so the release command works without exporting env vars every time.
The helpers also read `finance_dashboard/.env` if you prefer one repo-root env file for all release settings.
