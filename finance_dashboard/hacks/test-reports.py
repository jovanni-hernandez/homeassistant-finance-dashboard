#!/usr/bin/env python3
"""Checks year-end balance reconstruction, grouping, and export formats."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-reports-", dir=TEST_ROOT)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import init_db, save_simplefin_data  # noqa: E402
from models import Account, AccountBalanceSnapshot, AccountDataCoverage, SessionLocal, Transaction  # noqa: E402
from reports import (  # noqa: E402
    build_year_end_report,
    create_excel_report,
    create_pdf_report,
    group_year_end_accounts,
)

init_db()


class YearEndReportTests(unittest.TestCase):
    YEAR = 2024
    CUTOFF = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())

    def setUp(self):
        db = SessionLocal()
        try:
            db.query(AccountBalanceSnapshot).delete()
            db.query(Transaction).delete()
            db.query(AccountDataCoverage).delete()
            db.query(Account).delete()
            db.commit()
        finally:
            db.close()

    def add_account(self, account_id, *, provider="Capital One", name="Checking", kind="bank_account", currency="USD"):
        db = SessionLocal()
        try:
            db.add(Account(
                id=account_id,
                org=provider,
                name=name,
                balance="0.00",
                currency=currency,
                account_type=kind,
            ))
            db.commit()
        finally:
            db.close()

    def add_snapshot(self, account_id, balance, captured_at):
        db = SessionLocal()
        try:
            db.add(AccountBalanceSnapshot(
                account_id=account_id,
                balance=str(balance),
                currency="USD",
                captured_at=datetime.fromtimestamp(captured_at, timezone.utc).replace(tzinfo=None),
            ))
            db.commit()
        finally:
            db.close()

    def add_coverage(self, account_id, start, end, status="complete"):
        db = SessionLocal()
        try:
            db.add(AccountDataCoverage(
                account_id=account_id,
                start_ts=start,
                end_ts=end,
                status=status,
                source="test",
            ))
            db.commit()
        finally:
            db.close()

    def add_transaction(self, account_id, tx_id, when, amount, pending=False):
        db = SessionLocal()
        try:
            db.add(Transaction(
                id=tx_id,
                account_id=account_id,
                date=when,
                amount=str(amount),
                payee="Test transaction",
                description="",
                pending=pending,
            ))
            db.commit()
        finally:
            db.close()

    def test_reconstructs_year_end_from_later_snapshot_and_posted_transactions(self):
        self.add_account("account-a")
        anchor = self.CUTOFF + 3 * 86400
        self.add_snapshot("account-a", "1065.00", anchor)
        self.add_coverage("account-a", self.CUTOFF, anchor + 1)
        self.add_transaction("account-a", "tx-a", self.CUTOFF, "100.00")
        self.add_transaction("account-a", "tx-b", self.CUTOFF + 86400, "-35.00")
        self.add_transaction("account-a", "tx-pending", self.CUTOFF + 86400, "500.00", pending=True)

        report = build_year_end_report(self.YEAR, "UTC")

        self.assertEqual(report["accounts"][0]["status"], "verified")
        self.assertEqual(report["accounts"][0]["balance"], "1000.00")
        self.assertEqual(report["timezone"], "UTC")

    def test_missing_coverage_never_returns_a_balance(self):
        self.add_account("account-a")
        anchor = self.CUTOFF + 4 * 86400
        self.add_snapshot("account-a", "500.00", anchor)
        self.add_coverage("account-a", self.CUTOFF + 100, anchor + 1)

        account = build_year_end_report(self.YEAR, "UTC")["accounts"][0]

        self.assertEqual(account["status"], "unverified")
        self.assertIsNone(account["balance"])

    def test_reconstructs_year_end_from_earlier_snapshot(self):
        self.add_account("account-a")
        anchor = self.CUTOFF - 2 * 86400
        self.add_snapshot("account-a", "900.00", anchor)
        self.add_coverage("account-a", anchor, self.CUTOFF)
        self.add_transaction("account-a", "tx-a", self.CUTOFF - 86400, "25.00")
        self.add_transaction("account-a", "tx-b", self.CUTOFF - 3600, "75.00")

        account = build_year_end_report(self.YEAR, "UTC")["accounts"][0]

        self.assertEqual(account["status"], "verified")
        self.assertEqual(account["balance"], "1000.00")

    def test_accounts_without_a_dated_snapshot_are_unavailable(self):
        self.add_account("account-a")

        account = build_year_end_report(self.YEAR, "UTC")["accounts"][0]

        self.assertEqual(account["status"], "unavailable")
        self.assertIsNone(account["balance"])

    def test_snapshot_write_is_encrypted_and_uses_supplied_sync_time(self):
        save_simplefin_data({
            "accounts": [{
                "id": "account-a",
                "org": "Capital One",
                "name": "Checking",
                "balance": "123.45",
                "currency": "USD",
                "transactions": [],
            }],
        }, snapshot_account_ids={"account-a"}, snapshot_at=self.CUTOFF)

        db = SessionLocal()
        try:
            snapshot = db.query(AccountBalanceSnapshot).filter_by(account_id="account-a").one()
            self.assertEqual(snapshot.balance, "123.45")
            self.assertNotEqual(snapshot._balance, "123.45")
            self.assertEqual(int(snapshot.captured_at.replace(tzinfo=timezone.utc).timestamp()), self.CUTOFF)
        finally:
            db.close()

    def test_grouping_uses_provider_type_and_currency_and_keeps_unclassified_separate(self):
        accounts = [
            {"provider": "Capital One", "account_name": "Checking 1", "account_type": "bank_account", "account_type_label": "Bank / cash", "currency": "USD", "balance": "100", "status": "verified"},
            {"provider": " capital   one ", "account_name": "Checking 2", "account_type": "bank_account", "account_type_label": "Bank / cash", "currency": "USD", "balance": "200", "status": "verified"},
            {"provider": "Capital One", "account_name": "Euro", "account_type": "bank_account", "account_type_label": "Bank / cash", "currency": "EUR", "balance": "50", "status": "verified"},
            {"provider": "Capital One", "account_name": "Unknown 1", "account_type": "unclassified", "account_type_label": "Unclassified", "currency": "USD", "balance": "10", "status": "verified"},
            {"provider": "Capital One", "account_name": "Unknown 2", "account_type": "unclassified", "account_type_label": "Unclassified", "currency": "USD", "balance": "20", "status": "verified"},
        ]

        grouped = group_year_end_accounts(accounts, True)

        usd_group = next(row for row in grouped if row["account_type"] == "bank_account" and row["currency"] == "USD")
        self.assertEqual(usd_group["balance"], "300")
        self.assertEqual(len(usd_group["member_accounts"]), 2)
        self.assertEqual(sum(row["account_type"] == "unclassified" for row in grouped), 2)
        self.assertEqual(sum(row["currency"] == "EUR" for row in grouped), 1)

    def test_grouped_total_is_hidden_if_any_member_is_unverified(self):
        grouped = group_year_end_accounts([
            {"provider": "Capital One", "account_name": "One", "account_type": "bank_account", "account_type_label": "Bank / cash", "currency": "USD", "balance": "100", "status": "verified"},
            {"provider": "Capital One", "account_name": "Two", "account_type": "bank_account", "account_type_label": "Bank / cash", "currency": "USD", "balance": None, "status": "unverified"},
        ], True)

        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["status"], "unverified")
        self.assertIsNone(grouped[0]["balance"])

    def test_exports_are_valid_pdf_and_xlsx_files(self):
        report = {
            "year": self.YEAR,
            "timezone": "UTC",
            "as_of": f"December 31, {self.YEAR}",
            "group_accounts": True,
            "methodology": "Balances are calculated from verified data.",
            "report_note": "Derived from linked account data.",
            "accounts": [{
                "provider": "Capital One",
                "account_name": "Checking",
                "account_type_label": "Bank / cash",
                "currency": "USD",
                "balance": "1234.56",
                "status": "verified",
                "status_note": "Verified account history.",
            }],
            "rows": [{
                "provider": "Capital One",
                "account_name": "Checking",
                "account_type_label": "Bank / cash",
                "currency": "USD",
                "balance": "1234.56",
                "status": "verified",
                "status_note": "Verified account history.",
                "member_accounts": [{"account_name": "Checking"}],
            }],
        }

        excel = create_excel_report(report)
        self.assertTrue(excel.startswith(b"PK"))
        workbook = load_workbook(BytesIO(excel), data_only=True)
        self.assertEqual(workbook.sheetnames, ["Year-end summary", "Account details"])
        self.assertEqual(workbook["Year-end summary"]["E6"].value, 1234.56)
        self.assertEqual(workbook["Account details"]["E5"].value, 1234.56)
        self.assertTrue(create_pdf_report(report).startswith(b"%PDF-"))

    def test_current_or_future_year_and_unknown_timezone_are_rejected(self):
        with self.assertRaises(ValueError):
            build_year_end_report(datetime.now(timezone.utc).year, "UTC")
        with self.assertRaises(ValueError):
            build_year_end_report(self.YEAR, "Not/A_Real_Zone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
