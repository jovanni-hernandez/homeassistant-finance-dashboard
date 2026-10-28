#!/usr/bin/env python3
"""Unit checks for account classification and safe net-worth inputs."""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-account-types-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import database  # noqa: E402
import simplefin  # noqa: E402
from database import init_db, save_simplefin_data, set_account_type  # noqa: E402
from models import Account, SessionLocal, Setting, Transaction  # noqa: E402

init_db()


class AccountTypeTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(Account).delete()
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()

    def test_safe_type_suggestions_use_metadata_and_explicit_names_not_sign_alone(self):
        self.assertEqual(
            simplefin.suggest_account_type({"org": {"name": "Capital One"}, "name": "Venture X"}),
            "credit_card",
        )
        self.assertEqual(simplefin.suggest_account_type({"name": "IRA Brokerage"}), "investment")
        self.assertEqual(simplefin.suggest_account_type({"name": "Mortgage Loan"}), "loan")
        self.assertEqual(simplefin.suggest_account_type({"name": "Checking"}), "bank_account")
        self.assertEqual(simplefin.suggest_account_type({"name": "Mystery", "balance": "-4000.00"}), "unclassified")

    def test_classification_migrates_old_accounts_and_manual_type_survives_sync(self):
        legacy_db = Path(TEST_DATA.name) / "legacy.sqlite"
        legacy_engine = create_engine(f"sqlite:///{legacy_db}")
        try:
            with legacy_engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE accounts (id TEXT PRIMARY KEY, org TEXT, name TEXT, balance TEXT, currency TEXT)"
                ))
                connection.execute(text(
                    "INSERT INTO accounts (id, org, name, balance, currency) VALUES ('old', 'Bank', 'Checking', '10.00', 'USD')"
                ))
            with patch.object(database, "engine", legacy_engine):
                database._ensure_account_type_columns()
            with legacy_engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT account_type, account_type_suggestion FROM accounts WHERE id = 'old'"
                )).one()
            self.assertEqual(tuple(row), ("unclassified", "unclassified"))
        finally:
            legacy_engine.dispose()

        save_simplefin_data({"accounts": [{
            "id": "venture",
            "org": {"name": "Capital One"},
            "name": "Venture X",
            "balance": "4000.00",
            "currency": "USD",
            "account_type_suggestion": "credit_card",
            "transactions": [],
        }]})
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.id == "venture").one()
            self.assertEqual(account.account_type, "unclassified")
            self.assertEqual(account.account_type_suggestion, "credit_card")
        finally:
            db.close()

        set_account_type("venture", "credit_card")
        save_simplefin_data({"accounts": [{
            "id": "venture",
            "org": {"name": "Capital One"},
            "name": "Venture X",
            "balance": "3900.00",
            "currency": "USD",
            "account_type_suggestion": "unclassified",
            "transactions": [],
        }]})
        db = SessionLocal()
        try:
            account = db.query(Account).filter(Account.id == "venture").one()
            self.assertEqual(account.account_type, "credit_card")
            self.assertEqual(account.balance, "3900.00")
        finally:
            db.close()

    def test_unknown_account_types_are_rejected(self):
        save_simplefin_data({"accounts": [{
            "id": "checking",
            "org": {"name": "Bank"},
            "name": "Checking",
            "balance": "10.00",
            "currency": "USD",
            "transactions": [],
        }]})
        with self.assertRaises(ValueError):
            set_account_type("checking", "maybe-debt")
        self.assertIsNone(set_account_type("missing", "loan"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
