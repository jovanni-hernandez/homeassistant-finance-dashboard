---
name: Finance Live Database
description: Query or change the deployed Finance Dashboard SQLite database through its guarded SQL stdin bridge.
---

# Finance Dashboard live database

Use `./hacks/finance-sql.sh` from the `finance_dashboard/` directory to run one SQL statement against the running Finance add-on. Do not open or modify the deployed SQLite file directly; the script sends a command through Home Assistant's authenticated Core WebSocket `hassio.app_stdin` service, and the Finance add-on executes it through its own SQLAlchemy engine. No Supervisor token is needed in `.env`.

## Read queries

Queries are read-only by default. Example:

```bash
./hacks/finance-sql.sh "SELECT id, date, finance_decrypt(payee), finance_decrypt(amount), category FROM transactions LIMIT 50"
```

The database stores several finance fields encrypted. Use `finance_decrypt(column)` for intended Finance fields and `finance_encrypt(value)` when writing encrypted values. Reads of encrypted `settings.value` are blocked to protect SimpleFIN and provider credentials; never attempt to retrieve, print, or relay secrets.

Results can include account balances, payees, descriptions, and other private financial data. Request only fields needed for the task, limit result size, and do not copy private values into logs, documentation, or unrelated responses. The bridge caps results at 200 rows / 256 KiB and execution time at 15 seconds.

## Writes

Writes require both `--write` and `--apply`:

```bash
./hacks/finance-sql.sh --write --apply "UPDATE transactions SET category = 'Groceries' WHERE id = 'REPLACE_WITH_ID'"
```

Before using write mode, show and review the exact statement and obtain user approval. Arbitrary SQL can alter or delete data, break application invariants, or damage the database. The bridge accepts one statement at a time and blocks `ATTACH`/`DETACH`, extension loading, and explicit transaction-control statements, but it is not a substitute for a backup. Never retry a timed-out write until its outcome has been checked.

For routine Finance edits, prefer the app's specific API/commands over raw SQL. Use raw SQL only when the user explicitly requests database-level work. If PIN protection is enabled, the command also needs `FINANCE_SESSION_TOKEN` from the ignored `.env`.
