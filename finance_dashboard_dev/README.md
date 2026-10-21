# Finance Dashboard Dev add-on

This is a dev launcher variant of the finance dashboard.

What it does:
- clones the finance dashboard repo into `/data/finance-dashboard-workspace`
- creates a persistent Python venv in `/data/finance-dashboard-venv`
- installs backend deps plus Selenium/browser tooling
- boots the dashboard from the cloned workspace

You can override the defaults with environment variables:
- `FINANCE_DEV_REPO_URL`
- `FINANCE_DEV_BRANCH`
- `FINANCE_DEV_WORKDIR`
- `FINANCE_DEV_VENV`
