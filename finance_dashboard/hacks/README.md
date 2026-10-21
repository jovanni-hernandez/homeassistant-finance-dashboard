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

## Manual HA refresh

Home Assistant's add-on store refresh is the equivalent of a repo metadata check. The underlying HA tooling exposes a `hab marketplace refresh <repository>` command, but the bridge can be flaky in this environment. Use the HA UI refresh button when needed, or run the matching `hab` command if available in your session.


## Repository refresh

```bash
./hacks/refresh-marketplace.sh
```

This mirrors the Home Assistant marketplace refresh button and retries transient failures with exponential backoff. If the shell is not authenticated, it will stop immediately and tell you to log in.
