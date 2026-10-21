# Finance Dashboard Dev add-on

This is a dev launcher variant of the finance dashboard.

What it does:
- clones the finance dashboard repo into `/data/finance-dashboard-workspace`
- serves a browser-based launcher on port 8000
- lets you run repo test commands from the UI or a shell box
- shows per-job logs in the browser

You can override the defaults with environment variables:
- `FINANCE_DEV_REPO_URL`
- `FINANCE_DEV_BRANCH`
- `FINANCE_DEV_WORKDIR`
