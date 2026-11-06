# Home Assistant Custom Finance Add-on Repository

This repository contains the **Personal Finance Dashboard** Home Assistant Add-on.

## Add-ons in this repository

- **Personal Finance Dashboard** (`finance_dashboard/`): FastAPI backend with SQLite primary storage, SimpleFIN aggregation, and a Vue 3 dashboard.

## Installation
1. In Home Assistant, go to **Settings** -> **Add-ons** -> **Add-on Store**.
2. Click the three dots in the top right -> **Repositories**.
3. Add: `https://github.com/jovanni-hernandez/homeassistant-finance-dashboard`
4. Install **Personal Finance Dashboard**.

## Finance Dashboard sensors

This repository also provides a separate Home Assistant integration for exposing Finance Dashboard data as sensors.

1. In HACS, open the top-right menu, choose **Custom repositories**, and add this repository as an **Integration**.
2. Download **Finance Dashboard Sensors**, then restart Home Assistant Core.
3. In **Settings → Devices & services**, add **Finance Dashboard**. Use `http://custom_finance:8000` as the internal URL and leave the token blank unless token authentication is enabled in the add-on options.

The integration creates per-currency net-worth and current-month cash-flow sensors, account-balance sensors, and sync/freshness sensors. The raw-transaction sensor is disabled by default; if enabled, it exposes at most 20 recent transactions in state attributes, which Home Assistant may retain in history and backups. See [`finance_dashboard/README.md`](finance_dashboard/README.md#home-assistant-sensors) for details.
