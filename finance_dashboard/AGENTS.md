# Finance Dashboard project guide

These instructions apply to the Home Assistant Finance Dashboard add-on in this directory.

## Project layout

- `backend/main.py` — FastAPI routes, startup/shutdown hooks, PIN/session checks, and the restricted Supervisor-stdin bridge.
- `backend/simplefin.py` — SimpleFIN connection, normalization, category matching, sync windows, and backfill cursor.
- `backend/database.py` and `backend/models.py` — SQLite persistence and ORM models.
- `backend/pin_auth.py` — optional PIN settings and session validation.
- `frontend/index.html` — Vue dashboard, transaction list, category editor, and settings.
- `frontend/vendor/` — locally bundled runtime libraries. Do not replace them with remote CDN dependencies.
- `config.yaml`, `Dockerfile`, and `run.sh` — Home Assistant add-on manifest, image, and entrypoint.
- `hacks/` — focused tests, release helpers, and browser certification.

## Safety and data handling

- This is a live finance application. Never print, commit, or put SimpleFIN tokens, PINs, API keys, transaction descriptions, or private payee data into logs or documentation.
- Do not read or modify Home Assistant `.storage/`, databases, or secrets files. Use Home Assistant MCP tools for runtime and Supervisor evidence.
- Keep the add-on ingress-only. `ingress_port: 8000` is internal; do not add a host `ports:` mapping.
- Preserve manually selected transaction categories during sync and rule updates. Only uncategorized transactions are automatically reclassified unless the user explicitly requests a merchant override.
- Treat sync freshness as separate from HTTP success. Report returned transaction count, newest returned date, whether the stored date advanced, and any incomplete backfill.
- Keep each SimpleFIN `/accounts` date range at or below 90 days. Cap all-account requests at 12 per UTC day per access URL and wait at least 10 seconds between requests; count failed attempts too. Persist the daily request ledger, global cursor, and per-account 12-month verified coverage/retry ranges. Existing accounts receive one initial 12-month coverage pass; new accounts get the same pass. Normal syncs continue on the existing six-hour schedule and recent one-day window after backfill. Coverage means a successful provider response, not evidence inferred from transaction activity. Never mark a range complete when SimpleFIN reports incomplete data; split `act.missingdata` ranges, preserve failed gaps, and resume within the daily budget. Flag accounts not successfully returned for 48 hours.
- Account type is separate from balance sign. Preserve the raw signed balance; net worth adds classified assets and subtracts the absolute balance of classified liabilities. Apply only high-confidence metadata/name suggestions automatically; keep ambiguous accounts unclassified and exclude them from net worth. Mark user-chosen types as manual and never overwrite them on sync.
- Keep manual asset records in their own persistent table, separate from SimpleFIN accounts and transactions. Include their current value in net worth by currency, but never include them in transaction filtering, Sankey, or sync updates.
- If Supervisor or host logs show storage I/O/media errors, do not retry image builds or filesystem repairs blindly. Preserve data and report the evidence first.

## Development and tests

Run commands from this add-on directory. The isolated test scripts use a disposable database; keep their test data out of `/data` when possible:

```bash
python3 -m py_compile backend/*.py hacks/test-*.py
bash -n run.sh hacks/*.sh
FINANCE_TEST_ARTIFACT_DIR=/dev/shm "$FINANCE_TEST_VENV/bin/python" hacks/test-recurring-analysis.py
FINANCE_TEST_ARTIFACT_DIR=/dev/shm "$FINANCE_TEST_VENV/bin/python" hacks/test-api-validation.py
FINANCE_TEST_ARTIFACT_DIR=/dev/shm "$FINANCE_TEST_VENV/bin/python" hacks/test-admin-stdin.py
FINANCE_TEST_ARTIFACT_DIR=/dev/shm "$FINANCE_TEST_VENV/bin/python" hacks/test-simplefin-sync.py
```

`validate-ui-selenium.sh` runs the suite and Selenium checks. For local pre-publish testing, set `FINANCE_DASHBOARD_URL=local`; this is not a substitute for authenticated Ingress validation.

## UI requirements

- Design mobile-first and verify the iPhone-sized viewport (390 × 844) first.
- Preserve accessible labels, readable long merchant text, category editing, search/filter behavior, and pagination.
- Any UI change requires a version bump, GitHub push, marketplace refresh when available, browser validation, and screenshots. Do not call a UI change complete until browser validation passes; report unavailable or blocked steps explicitly.
- Do not claim rewards, cashback, or points unless a real data source provides them.

## Release

- Bump the patch version in `config.yaml` for each release; do not reuse a version already pushed.
- Use the repository certification/release helpers and inspect their results. They require authenticated Home Assistant/GitHub credentials from the approved environment; never echo or copy those credentials.
- Finance add-on updates must use `backup: false` unless the user explicitly requests a backup. Verify the installed version and app startup after an update.
- A published GitHub commit is not the same as a deployed add-on; report source pushed, store-recognized, and installed versions separately.
