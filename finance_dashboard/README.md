# Personal Finance Dashboard for Home Assistant

View accounts and transactions, understand cash flow, and organize spending from a dashboard inside Home Assistant. The add-on connects through SimpleFIN and is opened through Home Assistant Ingress; it does not publish a separate host port.

## Features

- Account balances and cash-flow visualization across the full stored transaction history, loaded in pages.
- Account classification for bank/cash, investments, credit cards, and loans, with net-worth totals grouped by currency.
- Manual current-value assets for property and other items SimpleFIN does not cover, managed from a separate Assets menu and included in net worth.
- Searchable, category-filterable transaction list grouped into pending and posted dates.
- Editable transaction categories and automatic merchant rules. Manual category choices are preserved during sync.
- Recurring-payment and subscription insights.
- Optional account nicknames shown on account cards and transaction/Sankey details.
- Optional PIN lock and optional AI-assisted category suggestions.
- Sync status that reports the number of transactions returned and their newest date, so a successful request is not mistaken for fresh data.
- Optional Home Assistant sensors for balances, net worth, monthly cash flow, and sync status through a local custom integration.

## Install and connect

1. In Home Assistant, open **Settings → Add-ons → Add-on Store**.
2. Add `https://github.com/jovanni-hernandez/homeassistant-finance-dashboard` as an add-on repository, then find **Personal Finance Dashboard**.
3. Install and start the add-on, then open its web UI from Home Assistant.
4. In **Settings → SimpleFIN connection**, paste the setup token from SimpleFIN and choose **Claim token & sync**.

After connecting, the dashboard displays the last sync time, status, account count, and transaction count. Use **Sync now** to request an immediate refresh.

## How it works

1. **Connect:** The dashboard sends the SimpleFIN setup token to SimpleFIN to claim an access token. The access token is stored in the add-on's persistent settings; it is not meant to be pasted into support requests or logs.
2. **Fetch in bounded windows:** Sync requests are limited to one day or less to stay within SimpleFIN's date-range limit. A rolling recent-day request also catches transactions that post late.
3. **Backfill gaps:** The add-on remembers its last successfully saved sync cursor. When it detects missed time, it fetches consecutive one-day windows, saves each before advancing the cursor, and processes up to seven backfill windows per run. Longer gaps resume on the next six-hour sync.
4. **Backfill new accounts:** When a newly linked account is first detected, the current rolling-day request plus consecutive one-day requests cover its preceding 30 days. The older windows run immediately in batches of seven requests; progress is saved after each successful window so an interruption can resume later.
5. **Store and categorize:** Normalized accounts and transactions are saved in the add-on's local SQLite database. Merchant rules categorize uncategorized transactions; a category you selected manually is preserved.
6. **Display:** The dashboard reads the stored data to render balances, cash flow, grouped transactions, and recurring-payment insights. It shows the newest transaction date returned by SimpleFIN and whether that date advanced.

The six-hour schedule can make repeated requests, but it cannot make a bank or SimpleFIN provide transactions that are not present in their response. A sync can complete successfully while the newest transaction date stays unchanged; the dashboard reports that as **Freshness NOT verified**.

## Home Assistant sensors

The add-on exposes a read-only metrics endpoint on its internal network. It does not publish a host port and does not use MQTT. The separate Finance Dashboard integration polls this endpoint every 15 minutes and creates sensors for:

- Assets, liabilities, and net worth by currency.
- Current-month income, spending, and net cash flow by currency. Pending transactions and transfers are excluded.
- Account balances, connection status, last sync time/status, freshness (whether the newest transaction date advanced), and account/transaction counts.

### Install the integration

1. In HACS, open **Integrations → Custom repositories** and add this repository as an **Integration**.
2. Download **Finance Dashboard Sensors**, then restart Home Assistant Core.
3. Open **Settings → Devices & services → Add integration → Finance Dashboard**.
4. Enter the internal add-on URL. The default is `http://custom_finance:8000`; the integration appends `/api/ha-metrics` automatically. Leave the token blank unless you enable token authentication below.

The integration is hosted in `custom_components/finance_dashboard/` in this repository. The add-on itself remains installed and updated separately through the Home Assistant Add-on Store.

### Optional endpoint authentication

By default, the endpoint trusts the add-on's internal network. Other add-ons on that network may also be able to reach it. To require a token, set `ha_metrics_api_token` in the Finance Dashboard add-on configuration and enter the same value in the integration setup (or reconfigure the integration). The add-on masks the option in its UI. Leaving it blank disables token authentication. This endpoint is read-only and does not return transaction data unless the transaction option is enabled.

### Optional raw transaction sensor

The raw transaction sensor is off by default. Enable **Expose the 20 most recent raw transactions** in the integration's **Configure** options to create it. Its attributes contain up to 20 recent records, including transaction and account identifiers, dates, amounts, payees, descriptions, categories, and pending status. Home Assistant may retain these attributes in recorder history and backups, so enable this only if that retention is acceptable.

## Categories and optional AI

Edit automatic rules in **Settings → Category rules**. You can add custom categories and patterns. When you manually change a transaction category, choose whether it applies only to that entry, to future matches, or to future matches plus existing matches. For a future rule, choose Merchant/payee, Description, or Both; Both saves both patterns and either can match. Existing-match updates show the affected count and replace every matching transaction's category, including categories previously chosen manually. New rules apply during future syncs. Optional AI suggestions require configuring a provider and model. Review that provider's privacy terms before enabling it because transaction context may be sent to the selected service.

## Account types and net worth

SimpleFIN reports balances but does not define a standard account-type field. The dashboard automatically classifies clear provider metadata or account-name clues, such as a bank account named “Checking” or a matching credit-card product. Ambiguous accounts stay unclassified and are excluded from net-worth totals until classified. You can review or change any type in **Settings → Account types and net worth**; manual choices are preserved on later syncs.

Bank/cash and investment balances count as assets. Credit cards, loans, and other liabilities count as debts; debt balances are subtracted from assets even if the institution reports the amount owed as positive. Negative asset balances still reduce net worth. Totals are kept separate by currency; the dashboard does not convert between currencies. Imported balances are preserved when you choose an account type.

You can set an optional nickname for each account in the same settings section. Nicknames are saved locally, retained during sync, and used in transaction details and cash-flow views; the bank-provided account name remains available in settings.

Use the **Assets** menu to add, edit, or remove manual assets such as a home. Each entry has a name, current value, and currency. Values are stored separately from SimpleFIN accounts, included in net worth for their currency, and excluded from transaction and cash-flow views. A mortgage is tracked separately as a liability if you want it included in net worth. Manual assets store a current value only; they do not keep valuation history.

## Privacy and security

- The dashboard is accessed through Home Assistant Ingress and does not expose an add-on host port.
- The application stores data in its persistent add-on storage. Selected sensitive field values and settings are encrypted by the application; this does not mean the whole SQLite file is encrypted.
- SimpleFIN is an external financial-data service. Optional AI features may send transaction context to the provider you configure.
- Back up Home Assistant/add-on data regularly and verify that backups include the content you need.
- PIN protection is optional and configured in the dashboard's **PIN security** settings.

## Troubleshooting sync

- Read the sync message's returned transaction count and newest date. If the date did not advance, the request may have succeeded while the bank/SimpleFIN feed had no newer transactions.
- If backfill is still pending, later scheduled runs continue from the last successfully saved window.
- If Home Assistant shows a PIN prompt unexpectedly, reopen the add-on from Home Assistant to obtain a fresh Ingress session. Do not assume an Ingress/session error means a PIN was configured.
- For persistent connection problems, check the add-on log for a redacted sync error and the SimpleFIN connection status in Settings. Never include access tokens or PINs when asking for help.
