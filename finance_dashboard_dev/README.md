# Finance Dashboard Dev add-on

This is a dev launcher variant of the finance dashboard.

What it does:
- clones the finance dashboard repo into `/data/finance-dashboard-workspace`
- serves a browser-based launcher on port 8000
- runs a small allowlist of repository checks from the UI (arbitrary shell input is disabled)
- shows per-job logs in the browser
- can embed screenshots from UI/Selenium/certify runs inline in each job card
- exposes the launcher through Home Assistant Ingress only; no host port is published

You can override the defaults with environment variables:
- `FINANCE_DEV_REPO_URL`
- `FINANCE_DEV_BRANCH`
- `FINANCE_DEV_WORKDIR`
