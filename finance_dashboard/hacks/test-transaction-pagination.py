#!/usr/bin/env python3
"""Checks transaction API pagination returns the full stored history."""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-pagination-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import main  # noqa: E402
from database import init_db  # noqa: E402
from models import Account, SessionLocal, Transaction  # noqa: E402

init_db()
db = SessionLocal()
try:
    db.add(Account(id="account-1", org="Test Bank", name="Checking", balance="0", currency="USD"))
    db.add_all([
        Transaction(
            id=f"tx-{index}",
            account_id="account-1",
            date=index,
            amount="-1.00",
            payee=f"Merchant {index}",
            description=f"Transaction {index}",
            category="Housing" if index == 1 else "Uncategorized",
            pending=False,
        )
        for index in range(1, 124)
    ])
    db.commit()
finally:
    db.close()


class TransactionPaginationTests(unittest.TestCase):
    def test_pages_cover_all_transactions_without_duplicates(self):
        first = main.api_get_transactions(limit=100, offset=0)
        second = main.api_get_transactions(limit=100, offset=100)
        rows = first["transactions"] + second["transactions"]

        self.assertEqual(first["total"], 123)
        self.assertEqual(second["total"], 123)
        self.assertEqual((first["offset"], len(first["transactions"])), (0, 100))
        self.assertEqual((second["offset"], len(second["transactions"])), (100, 23))
        self.assertEqual(len({transaction["id"] for transaction in rows}), 123)
        self.assertEqual(rows[0]["date"], 123)
        self.assertEqual(rows[-1]["date"], 1)

    def test_default_page_size_covers_normal_sized_history(self):
        response = main.api_get_transactions(limit=500, offset=0)
        self.assertEqual(response["total"], 123)
        self.assertEqual(len(response["transactions"]), 123)


if __name__ == "__main__":
    unittest.main(verbosity=2)
