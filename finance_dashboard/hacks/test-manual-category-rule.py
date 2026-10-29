#!/usr/bin/env python3
"""Checks one-off transaction categories and opt-in future-match rules."""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-category-rule-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import (  # noqa: E402
    get_category_rules_value,
    init_db,
    set_category_rules_value,
    set_transaction_category,
)
from models import Account, SessionLocal, Setting, Transaction  # noqa: E402
import simplefin  # noqa: E402

init_db()


class ManualCategoryRuleTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(Account).delete()
            db.query(Setting).filter(Setting.key == "category_rules_json").delete()
            db.add(Account(id="test-account", org="Test Bank", name="Checking", balance="100.00", currency="USD"))
            db.add_all([
                Transaction(
                    id="target-transaction",
                    account_id="test-account",
                    date=1760000000,
                    amount="-12.00",
                    payee="Coffee Shop",
                    description="Morning coffee",
                    category="Dining",
                    pending=False,
                ),
                Transaction(
                    id="existing-match",
                    account_id="test-account",
                    date=1759000000,
                    amount="-8.00",
                    payee="Coffee Shop",
                    description="Previous visit",
                    category="Dining",
                    pending=False,
                ),
                Transaction(
                    id="description-match",
                    account_id="test-account",
                    date=1758500000,
                    amount="-6.00",
                    payee="Coffee Roasters",
                    description="Coffee Shop purchase",
                    category="Groceries",
                    pending=False,
                ),
                Transaction(
                    id="already-correct-match",
                    account_id="test-account",
                    date=1758400000,
                    amount="-5.00",
                    payee="Coffee Shop",
                    description="Already correctly categorized",
                    category="Shopping",
                    pending=False,
                ),
                Transaction(
                    id="no-merchant-text",
                    account_id="test-account",
                    date=1758000000,
                    amount="-3.00",
                    payee="",
                    description="",
                    category="Uncategorized",
                    pending=False,
                ),
            ])
            db.commit()
        finally:
            db.close()

    def transaction_category(self, transaction_id):
        db = SessionLocal()
        try:
            return db.query(Transaction).filter(Transaction.id == transaction_id).one().category
        finally:
            db.close()

    def test_one_off_edit_does_not_create_a_rule(self):
        self.assertTrue(set_transaction_category(
            "target-transaction",
            "Shopping",
            default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
        ))

        self.assertEqual(self.transaction_category("target-transaction"), "Shopping")
        self.assertIsNone(get_category_rules_value())

    def test_opt_in_rule_preserves_existing_rules_and_only_updates_selected_entry(self):
        existing_rules = [
            {"category": "Groceries", "patterns": ["market"]},
            {"category": "Dining", "patterns": ["cafe", "coffee shop"]},
        ]
        set_category_rules_value(json.dumps(existing_rules))

        self.assertTrue(set_transaction_category(
            "target-transaction",
            "Shopping",
            apply_to_future_matches=True,
            default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
        ))

        saved_rules = json.loads(get_category_rules_value())
        self.assertEqual(saved_rules[0], {"category": "Shopping", "patterns": ["Coffee Shop"]})
        self.assertEqual(saved_rules[1], {"category": "Groceries", "patterns": ["market"]})
        self.assertEqual(saved_rules[2], {"category": "Dining", "patterns": ["cafe"]})
        self.assertEqual(self.transaction_category("target-transaction"), "Shopping")
        self.assertEqual(self.transaction_category("existing-match"), "Dining")
        self.assertEqual(self.transaction_category("description-match"), "Groceries")
        self.assertEqual(self.transaction_category("already-correct-match"), "Shopping")
        self.assertEqual(
            simplefin.categorize_transaction(
                {"payee": "Coffee Shop #1234", "description": "Future visit"},
                rules=simplefin._load_category_rules(),
            ),
            "Shopping",
        )

    def test_explicit_retroactive_choice_updates_all_matching_categories(self):
        result = set_transaction_category(
            "target-transaction",
            "Shopping",
            apply_to_future_matches=True,
            apply_to_existing_matches=True,
            default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
        )

        self.assertEqual(result["updated_existing_count"], 2)
        self.assertEqual(self.transaction_category("target-transaction"), "Shopping")
        self.assertEqual(self.transaction_category("existing-match"), "Shopping")
        self.assertEqual(self.transaction_category("description-match"), "Shopping")
        self.assertEqual(self.transaction_category("already-correct-match"), "Shopping")

    def test_existing_match_updates_require_a_future_rule(self):
        with self.assertRaisesRegex(ValueError, "requires saving a future-match rule"):
            set_transaction_category(
                "target-transaction",
                "Shopping",
                apply_to_existing_matches=True,
                default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
            )
        self.assertEqual(self.transaction_category("target-transaction"), "Dining")

    def test_failed_rule_creation_rolls_back_the_category_edit(self):
        with self.assertRaisesRegex(ValueError, "no payee or description"):
            set_transaction_category(
                "no-merchant-text",
                "Shopping",
                apply_to_future_matches=True,
                default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
            )

        self.assertEqual(self.transaction_category("no-merchant-text"), "Uncategorized")
        self.assertIsNone(get_category_rules_value())

    def test_new_rule_uses_builtin_rules_when_no_saved_rules_exist(self):
        set_transaction_category(
            "target-transaction",
            "Shopping",
            apply_to_future_matches=True,
            default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
        )

        saved_rules = json.loads(get_category_rules_value())
        self.assertEqual(saved_rules[0], {"category": "Shopping", "patterns": ["Coffee Shop"]})
        self.assertTrue(any(rule["category"] == "Income" and "payroll" in rule["patterns"] for rule in saved_rules))
        self.assertTrue(any(rule["category"] == "Groceries" and "whole foods" in rule["patterns"] for rule in saved_rules))


if __name__ == "__main__":
    unittest.main(verbosity=2)
