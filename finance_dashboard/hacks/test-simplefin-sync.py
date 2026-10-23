#!/usr/bin/env python3
"""Checks SimpleFIN sync freshness reporting and periodic sync behavior."""
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
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-sync-unit-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import main  # noqa: E402
import simplefin  # noqa: E402
from database import get_setting, init_db, set_setting  # noqa: E402
from models import Account, SessionLocal, Setting, Transaction  # noqa: E402

init_db()


class SimpleFinSyncTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(Account).delete()
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()
        simplefin.set_simplefin_token("https://test-user:test-password@example.test/access")

    def seed_latest_transaction(self, timestamp: int):
        db = SessionLocal()
        try:
            db.add(Account(id="account-1", org="Test Bank", name="Checking", balance="0", currency="USD"))
            db.add(Transaction(
                id="existing-transaction",
                account_id="account-1",
                date=timestamp,
                amount="-1.00",
                payee="Test merchant",
                description="Test transaction",
                category="Other",
                pending=False,
            ))
            db.commit()
        finally:
            db.close()

    def payload(self, transactions):
        return {
            "accounts": [{
                "id": "account-1",
                "org": {"name": "Test Bank"},
                "name": "Checking",
                "balance": "0",
                "currency": "USD",
                "transactions": transactions,
            }]
        }

    def test_sync_reports_no_newer_date_when_provider_returns_stale_data(self):
        previous_timestamp = 1_700_000_000
        self.seed_latest_transaction(previous_timestamp)
        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([
            {"id": "existing-transaction", "date": previous_timestamp, "amount": "-1.00", "payee": "Test merchant"},
        ])):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["incoming_transaction_count"], 1)
        self.assertEqual(result["latest_incoming_transaction_date"], "2023-11-14")
        self.assertEqual(result["previous_latest_transaction_date"], "2023-11-14")
        self.assertFalse(result["newest_transaction_date_advanced"])
        self.assertIn("Freshness NOT verified", result["message"])
        self.assertIn("no date newer than the previously stored latest", result["message"])

    def test_sync_reports_when_latest_transaction_date_advances(self):
        previous_timestamp = 1_700_000_000
        self.seed_latest_transaction(previous_timestamp)
        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([
            {"id": "new-transaction", "date": previous_timestamp + 86_400, "amount": "-2.00", "payee": "Test merchant"},
        ])):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(result["incoming_transaction_count"], 1)
        self.assertEqual(result["latest_incoming_transaction_date"], "2023-11-15")
        self.assertTrue(result["newest_transaction_date_advanced"])
        self.assertIn("Freshness verified: the newest transaction date advanced", result["message"])

    def test_sync_reports_empty_provider_response(self):
        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(result["incoming_transaction_count"], 0)
        self.assertIsNone(result["latest_incoming_transaction_date"])
        self.assertIn("Freshness NOT verified: SimpleFIN returned no transactions", result["message"])

    def test_sync_requests_exactly_one_day(self):
        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])) as fetch:
            simplefin.fetch_and_sync_simplefin()

        start_timestamp = fetch.call_args.kwargs["start_ts"]
        end_timestamp = fetch.call_args.kwargs["end_ts"]
        self.assertEqual(end_timestamp - start_timestamp, 24 * 60 * 60)

    def test_sync_backfills_missed_days_with_contiguous_one_day_windows(self):
        now_timestamp = int(simplefin.datetime.now(simplefin.timezone.utc).timestamp())
        cursor = now_timestamp - (3 * simplefin.SYNC_WINDOW_SECONDS) - 3600
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(cursor))

        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])) as fetch:
            result = simplefin.fetch_and_sync_simplefin()

        calls = fetch.call_args_list
        self.assertEqual(result["backfill_pending"], False)
        self.assertGreaterEqual(result["sync_windows"], 3)
        self.assertLessEqual(result["sync_windows"], simplefin.SYNC_MAX_BACKFILL_WINDOWS_PER_RUN + 1)
        for call in calls:
            start_timestamp = call.kwargs["start_ts"]
            end_timestamp = call.kwargs["end_ts"]
            self.assertGreater(end_timestamp, start_timestamp)
            self.assertLessEqual(end_timestamp - start_timestamp, simplefin.SYNC_WINDOW_SECONDS)
        for previous, following in zip(calls, calls[1:]):
            self.assertEqual(previous.kwargs["end_ts"], following.kwargs["start_ts"])
        self.assertEqual(get_setting(simplefin.SYNC_CURSOR_SETTING_KEY), str(calls[-1].kwargs["end_ts"]))

    def test_long_backfill_is_bounded_and_resumes_from_saved_cursor(self):
        now_timestamp = int(simplefin.datetime.now(simplefin.timezone.utc).timestamp())
        cursor = now_timestamp - (12 * simplefin.SYNC_WINDOW_SECONDS)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(cursor))

        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])) as fetch:
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(result["backfill_pending"], True)
        self.assertEqual(result["sync_windows"], simplefin.SYNC_MAX_BACKFILL_WINDOWS_PER_RUN + 1)
        saved_cursor = int(get_setting(simplefin.SYNC_CURSOR_SETTING_KEY))
        self.assertEqual(saved_cursor, cursor + simplefin.SYNC_MAX_BACKFILL_WINDOWS_PER_RUN * simplefin.SYNC_WINDOW_SECONDS)
        self.assertGreater(result["backfill_remaining_days"], 0)

    def test_sync_interval_is_six_hours_and_scheduled_runner_calls_sync(self):
        self.assertEqual(simplefin.SYNC_HISTORY_DAYS, 1)
        self.assertEqual(simplefin.SYNC_OVERLAP_DAYS, 0)
        self.assertEqual(simplefin.SYNC_INTERVAL_SECONDS, 6 * 60 * 60)
        with patch.object(simplefin, "fetch_and_sync_simplefin", return_value={"status": "success"}) as sync:
            main._run_periodic_sync_once()
        sync.assert_called_once_with()


if __name__ == "__main__":
    unittest.main(verbosity=2)
