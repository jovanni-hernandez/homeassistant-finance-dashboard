#!/usr/bin/env python3
"""Unit checks for the Finance add-on stdin bridge and SQL guardrails."""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-stdin-unit-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = ":memory:"
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import init_db, set_category_rules_value  # noqa: E402
import main  # noqa: E402
from main import _finance_agent_stdin_message, _finance_agent_stdin_result  # noqa: E402
from models import Account, SessionLocal, Transaction  # noqa: E402

init_db()
db = SessionLocal()
try:
    db.add(Account(id="account-1", org="Bank", name="Checking", balance="100.00", currency="USD"))
    db.add_all([
        Transaction(id="tx-1", account_id="account-1", date=1, amount="-12.00", payee="Costco #123456", description="Store 123456", category="Uncategorized", pending=False),
        Transaction(id="tx-2", account_id="account-1", date=2, amount="-18.00", payee="Costco #987654", description="Store 987654", category="Groceries", pending=False),
        Transaction(id="tx-3", account_id="account-1", date=3, amount="-5.00", payee="Zelle Alex Example", description="Transfer", category="Uncategorized", pending=False),
        Transaction(id="tx-4", account_id="account-1", date=4, amount="-20.00", payee="Amy Lindborg", description="Dog walking", category="Other", pending=False),
    ])
    db.commit()
finally:
    db.close()


class FinanceAgentStdinTests(unittest.TestCase):
    def setUp(self):
        set_category_rules_value("")
        db = SessionLocal()
        try:
            db.query(Transaction).filter(Transaction.id == "tx-1").update({Transaction.category: "Uncategorized"})
            db.query(Transaction).filter(Transaction.id == "tx-2").update({Transaction.category: "Groceries"})
            db.query(Transaction).filter(Transaction.id == "tx-3").update({Transaction.category: "Uncategorized"})
            db.query(Transaction).filter(Transaction.id == "tx-4").update({Transaction.category: "Other"})
            db.commit()
        finally:
            db.close()

    def test_audit_returns_only_merchant_category_and_count(self):
        result = _finance_agent_stdin_result({"command": "merchant_audit", "offset": 0, "limit": 10})
        self.assertTrue(result["ok"])
        costco = next(item for item in result["merchants"] if item["merchant"].casefold() == "costco")
        self.assertEqual(costco["count"], 2)
        self.assertEqual(costco["categories"], {"Uncategorized": 1, "Groceries": 1})
        self.assertEqual(set(costco), {"merchant", "count", "categories"})

    def test_apply_rules_reclassifies_only_uncategorized_transactions(self):
        result = _finance_agent_stdin_result({
            "command": "apply_category_rules",
            "rules": [{"category": "Groceries", "patterns": ["costco"]}],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["reclassified_uncategorized"], 1)
        db = SessionLocal()
        try:
            transactions = {tx.id: tx.category for tx in db.query(Transaction).all()}
        finally:
            db.close()
        self.assertEqual(transactions["tx-1"], "Groceries")
        self.assertEqual(transactions["tx-2"], "Groceries")
        self.assertEqual(transactions["tx-3"], "Uncategorized")

    def test_invalid_rules_and_missing_finance_pin_session_are_rejected(self):
        invalid = _finance_agent_stdin_result({"command": "apply_category_rules", "rules_json": "[]x"})
        self.assertFalse(invalid["ok"])
        with patch("main.pin_auth.pin_is_required", return_value=True), patch("main.pin_auth.validate_session_token", return_value=False):
            denied = _finance_agent_stdin_result({"command": "merchant_audit"})
        self.assertFalse(denied["ok"])

    def test_explicit_user_merchant_override_replaces_existing_category(self):
        result = _finance_agent_stdin_result({
            "command": "apply_category_rules",
            "rules": [{"category": "Pets", "patterns": ["dog walking"]}],
            "merchant_overrides": [{"merchant": "Amy Lindborg", "category": "Pets"}],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["explicit_merchant_overrides"], 1)
        db = SessionLocal()
        try:
            category = db.query(Transaction).filter(Transaction.id == "tx-4").one().category
        finally:
            db.close()
        self.assertEqual(category, "Pets")

    def test_stdin_accepts_supervisor_json_payload_without_newline(self):
        result = _finance_agent_stdin_message(b'{"command":"merchant_audit","offset":0,"limit":1}')
        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "merchant_audit")

    def test_stdin_rejects_oversized_payload(self):
        result = _finance_agent_stdin_message(b"x" * (main.FINANCE_STDIN_MAX_BYTES + 1))
        self.assertFalse(result["ok"])

    def test_sql_reads_encrypted_finance_values_but_not_settings_secrets(self):
        result = _finance_agent_stdin_result({
            "command": "sql",
            "request_id": "a" * 32,
            "sql": "SELECT id, finance_decrypt(payee), finance_decrypt(amount), category FROM transactions WHERE id = 'tx-1'",
        })
        self.assertTrue(result["ok"])
        self.assertFalse(result["committed"])
        self.assertEqual(result["rows"], [["tx-1", "Costco #123456", "-12.00", "Uncategorized"]])
        settings_secret = _finance_agent_stdin_result({
            "command": "sql",
            "request_id": "1" * 32,
            "sql": "SELECT finance_decrypt(value) FROM settings",
        })
        self.assertFalse(settings_secret["ok"])

    def test_sql_is_read_only_unless_apply_is_requested(self):
        denied = _finance_agent_stdin_result({
            "command": "sql",
            "request_id": "b" * 32,
            "sql": "UPDATE transactions SET category = 'Pets' WHERE id = 'tx-1'",
        })
        self.assertFalse(denied["ok"])
        updated = _finance_agent_stdin_result({
            "command": "sql",
            "request_id": "c" * 32,
            "sql": "UPDATE transactions SET category = 'Pets' WHERE id = 'tx-1'",
            "apply": True,
        })
        self.assertTrue(updated["ok"])
        self.assertTrue(updated["committed"])
        self.assertEqual(updated["affected_rows"], 1)
        db = SessionLocal()
        try:
            self.assertEqual(db.query(Transaction).filter(Transaction.id == "tx-1").one().category, "Pets")
        finally:
            db.close()

    def test_sql_blocks_database_escape_multiple_statements_and_manual_transactions(self):
        statements = [
            ("ATTACH DATABASE ':memory:' AS other", True),
            ("SELECT 1; SELECT 2", False),
            ("/* comment */ COMMIT", False),
        ]
        for index, (statement, apply) in enumerate(statements, start=2):
            result = _finance_agent_stdin_result({
                "command": "sql",
                "request_id": f"{index:032x}",
                "sql": statement,
                "apply": apply,
            })
            self.assertFalse(result["ok"], statement)

    def test_sql_result_event_is_request_scoped_and_does_not_log_rows(self):
        result = {"ok": True, "command": "sql", "request_id": "f" * 32, "rows": [["test-only"]]}
        with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "unit-test-supervisor-token"}), \
             patch("main.urllib.request.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.status = 200
            self.assertTrue(main._publish_finance_sql_result(result))
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/events/finance_sql_result_" + "f" * 32))
        self.assertEqual(request.get_header("Authorization"), "Bearer unit-test-supervisor-token")

    def test_sql_result_event_fails_closed_without_home_assistant_api_access(self):
        with patch.dict(os.environ, {}, clear=True), patch("main.urllib.request.urlopen") as urlopen:
            self.assertFalse(main._publish_finance_sql_result({"request_id": "a" * 32}))
            urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
