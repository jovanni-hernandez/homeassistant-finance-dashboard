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
- Keep every SimpleFIN request at or below 24 hours. Persist the sync cursor only after a window's response has been saved successfully. Backfill at most seven sequential windows per run, then resume on a later run.
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
