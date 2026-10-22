#!/usr/bin/env python3
"""Unit checks for the restricted Supervisor-stdin finance bridge."""
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
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import init_db, set_category_rules_value  # noqa: E402
from main import _finance_agent_stdin_result  # noqa: E402
from models import Account, SessionLocal, Transaction  # noqa: E402

init_db()
db = SessionLocal()
try:
    db.add(Account(id="account-1", org="Bank", name="Checking", balance="100.00", currency="USD"))
    db.add_all([
        Transaction(id="tx-1", account_id="account-1", date=1, amount="-12.00", payee="Costco #123456", description="Store 123456", category="Uncategorized", pending=False),
        Transaction(id="tx-2", account_id="account-1", date=2, amount="-18.00", payee="Costco #987654", description="Store 987654", category="Groceries", pending=False),
        Transaction(id="tx-3", account_id="account-1", date=3, amount="-5.00", payee="Zelle Alex Example", description="Transfer", category="Uncategorized", pending=False),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
