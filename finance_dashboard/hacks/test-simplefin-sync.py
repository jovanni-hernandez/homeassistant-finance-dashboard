#!/usr/bin/env python3
"""Checks SimpleFIN sync freshness reporting and periodic sync behavior."""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

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
from models import Account, AccountDataCoverage, SessionLocal, Setting, Transaction  # noqa: E402

init_db()


class SimpleFinSyncTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(AccountDataCoverage).delete()
            db.query(Account).delete()
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()
        simplefin.set_simplefin_token("https://test-user:test-password@example.test/access")

    def seed_latest_transaction(self, timestamp: int, *, history_migrated: bool = True):
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
            if history_migrated:
                set_setting(simplefin.ACCOUNT_BACKFILL_VERSION_KEY, simplefin.ACCOUNT_BACKFILL_VERSION)
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

    def new_account_payload(self, start_ts, account_ids=("new-account",)):
        return {
            "accounts": [
                {
                    "id": account_id,
                    "org": {"name": "New Bank"},
                    "name": f"Checking {account_id}",
                    "balance": "100.00",
                    "currency": "USD",
                    "transactions": [{
                        "id": f"{account_id}-{start_ts}",
                        "date": start_ts + 1,
                        "amount": "-1.00",
                        "payee": "Test merchant",
                        "description": "Daily test transaction",
                    }],
                }
                for account_id in account_ids
            ]
        }

    def test_connection_error_only_marks_accounts_on_matching_connection(self):
        payload = {
            "errlist": [{"code": "con.auth", "msg": "Authentication required", "conn_id": "connection-private-b"}],
            "accounts": [
                {"id": "account-private-a", "conn_id": "connection-private-a"},
                {"id": "account-private-b", "conn_id": "connection-private-b"},
            ],
        }

        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=payload):
            with self.assertLogs("finance_dashboard", level="INFO") as captured:
                _, errors, tracked_ids, returned_ids = simplefin._fetch_tracked_payload(
                    "https://unused.example/access",
                    1_700_000_000,
                    1_700_086_400,
                    {"account-private-a", "account-private-b"},
                    "test_connection_scope",
                )

        self.assertEqual(tracked_ids, {"account-private-a", "account-private-b"})
        self.assertEqual(returned_ids, {"account-private-a", "account-private-b"})
        self.assertEqual(errors, {"account-private-b": "con.auth"})
        log_text = "\n".join(captured.output)
        self.assertIn("matching_accounts=1", log_text)
        self.assertIn('provider_error_counts={"con.auth": 1}', log_text)
        self.assertNotIn("account-private", log_text)
        self.assertNotIn("connection-private", log_text)

    def test_connection_error_matches_mx_prefixed_account_connection_id(self):
        payload = {
            "errlist": [{"code": "con.auth", "conn_id": "MBR-connection-a"}],
            "accounts": [
                {"id": "account-a", "conn_id": "MX-MBR-connection-a"},
                {"id": "account-b", "conn_id": "MX-MBR-connection-b"},
            ],
        }

        errors = simplefin._coverage_errors(payload, {"account-a", "account-b"})

        self.assertEqual(errors, {"account-a": "con.auth"})

    def test_routine_cursor_advances_and_leaves_auth_failure_for_manual_retry(self):
        now = int(simplefin._utc_now().timestamp())
        day = simplefin.SYNC_WINDOW_SECONDS
        cursor = now - (4 * day)
        window_end = cursor + day
        db = SessionLocal()
        try:
            db.add_all([
                Account(id="healthy-account", org="Test Bank", name="Healthy", balance="0", currency="USD"),
                Account(id="reauth-account", org="Test Bank", name="Needs reauth", balance="0", currency="USD"),
            ])
            db.commit()
        finally:
            db.close()
        set_setting(simplefin.ACCOUNT_BACKFILL_VERSION_KEY, simplefin.ACCOUNT_BACKFILL_VERSION)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(cursor))

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None, account_ids=None):
            if on_reserved:
                on_reserved()
            payload = {
                "accounts": [
                    {"id": "healthy-account", "conn_id": "MX-MBR-healthy", "name": "Healthy", "balance": "0", "currency": "USD", "transactions": []},
                    {"id": "reauth-account", "conn_id": "MX-MBR-reauth", "name": "Needs reauth", "balance": "0", "currency": "USD", "transactions": []},
                ]
            }
            if start_ts == cursor:
                payload["errlist"] = [{"code": "con.auth", "conn_id": "MBR-reauth", "msg": "Authentication required"}]
            return payload

        with patch.object(simplefin, "SYNC_MAX_BACKFILL_WINDOWS_PER_RUN", 1), patch.object(
            simplefin, "_fetch_simplefin_payload", side_effect=fetch_window
        ):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(int(get_setting(simplefin.SYNC_CURSOR_SETTING_KEY)), window_end)
        self.assertTrue(result["backfill_pending"])
        self.assertFalse(result["routine_retry_pending"])
        self.assertEqual(result["auth_failed_ranges"], 1)
        retry_ranges = simplefin._load_routine_retry_ranges()
        self.assertEqual(retry_ranges, [])

        db = SessionLocal()
        try:
            coverage = {
                row.account_id: row.status
                for row in db.query(AccountDataCoverage).filter_by(start_ts=cursor, end_ts=window_end).all()
            }
        finally:
            db.close()
        self.assertEqual(coverage, {"healthy-account": "complete", "reauth-account": "failed"})

    def test_routine_retry_fetches_only_the_affected_account_and_clears_on_success(self):
        now = int(simplefin._utc_now().timestamp())
        start = now - (3 * simplefin.SYNC_WINDOW_SECONDS)
        end = start + simplefin.SYNC_WINDOW_SECONDS
        db = SessionLocal()
        try:
            db.add(Account(id="reauth-account", org="Test Bank", name="Needs reauth", balance="0", currency="USD"))
            db.commit()
        finally:
            db.close()
        set_setting(simplefin.ACCOUNT_BACKFILL_VERSION_KEY, simplefin.ACCOUNT_BACKFILL_VERSION)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(now))
        simplefin._save_routine_retry_ranges([{
            "account_id": "reauth-account",
            "start": start,
            "end": end,
            "retry_after": 0,
            "attempts": 1,
            "error_code": "con.auth",
        }])
        account_filter_calls = []

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None, account_ids=None):
            if on_reserved:
                on_reserved()
            if account_ids is not None:
                account_filter_calls.append(account_ids)
                return {"accounts": [{
                    "id": "reauth-account",
                    "conn_id": "MX-MBR-reauth",
                    "name": "Needs reauth",
                    "balance": "0",
                    "currency": "USD",
                    "transactions": [],
                }]}
            return {"accounts": [{
                "id": "reauth-account",
                "conn_id": "MX-MBR-reauth",
                "name": "Needs reauth",
                "balance": "0",
                "currency": "USD",
                "transactions": [],
            }]}

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=fetch_window):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(account_filter_calls, [["reauth-account"]])
        self.assertEqual(simplefin._load_routine_retry_ranges(), [])
        self.assertFalse(result["routine_retry_pending"])
        db = SessionLocal()
        try:
            row = db.query(AccountDataCoverage).filter_by(
                account_id="reauth-account", start_ts=start, end_ts=end
            ).one()
        finally:
            db.close()
        self.assertEqual(row.status, "complete")

    def test_rolling_connection_error_does_not_hold_shared_recent_cursor(self):
        now = int(simplefin._utc_now().timestamp())
        initial_cursor = now - 10
        account_ids = {"healthy-account", "reauth-account"}
        db = SessionLocal()
        try:
            db.add_all([
                Account(id="healthy-account", org="Test Bank", name="Healthy", balance="0", currency="USD"),
                Account(id="reauth-account", org="Test Bank", name="Needs reauth", balance="0", currency="USD"),
            ])
            db.commit()
        finally:
            db.close()
        set_setting(simplefin.ACCOUNT_BACKFILL_VERSION_KEY, simplefin.ACCOUNT_BACKFILL_VERSION)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(initial_cursor))
        calls = []

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None, account_ids=None):
            calls.append((start_ts, end_ts))
            if on_reserved:
                on_reserved()
            return {
                "errlist": [{"code": "con.auth", "conn_id": "MBR-reauth"}],
                "accounts": [
                    {"id": "healthy-account", "conn_id": "MX-MBR-healthy", "name": "Healthy", "balance": "0", "currency": "USD", "transactions": []},
                    {"id": "reauth-account", "conn_id": "MX-MBR-reauth", "name": "Needs reauth", "balance": "0", "currency": "USD", "transactions": []},
                ],
            }

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=fetch_window):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(len(calls), 1)
        self.assertEqual(int(get_setting(simplefin.SYNC_CURSOR_SETTING_KEY)), calls[0][1])
        self.assertFalse(result["backfill_pending"])
        db = SessionLocal()
        try:
            coverage = {
                row.account_id: row.status
                for row in db.query(AccountDataCoverage).filter_by(start_ts=calls[0][0], end_ts=calls[0][1]).all()
            }
        finally:
            db.close()
        self.assertEqual(coverage, {"healthy-account": "complete", "reauth-account": "failed"})

    def test_simplefin_payload_supports_account_filtered_requests(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"accounts": []}
        budget = {
            "requests_last_24h": 1,
            "limit_24h": 24,
            "requests_remaining": 23,
            "next_request_at": None,
        }
        with patch.object(simplefin, "_reserve_simplefin_request", return_value=budget), patch.object(
            simplefin, "_write_sync_progress"
        ), patch.object(simplefin.requests, "get", return_value=response) as request:
            simplefin._fetch_simplefin_payload(
                simplefin.get_simplefin_token(),
                start_ts=1_700_000_000,
                end_ts=1_700_086_400,
                account_ids=["account-b", "account-a"],
            )

        self.assertEqual(request.call_args.kwargs["params"]["account"], ["account-a", "account-b"])

    def test_retry_queue_logs_progress_without_account_identifiers(self):
        now = int(simplefin._utc_now().timestamp())
        account_state = {
            "private-account-a": {
                "start": now - 100,
                "cursor": now - 50,
                "end": now + 100,
                "window_days": 90,
                "retry_after": 0,
            }
        }
        routine_ranges = [{
            "account_id": "private-account-b",
            "start": now - 200,
            "end": now - 100,
            "attempts": 2,
            "error_code": "con.auth",
            "retry_after": now + 600,
        }]

        with self.assertLogs("finance_dashboard", level="INFO") as info_logs:
            simplefin._log_sync_retry_queue_state("test", account_state, routine_ranges)
        info_text = "\n".join(info_logs.output)
        self.assertIn("account_backfill_queued=1 account_backfill_eligible=1", info_text)
        self.assertIn("routine_retry_queued=1 routine_retry_eligible=0", info_text)
        self.assertNotIn("private-account", info_text)

        with self.assertLogs("finance_dashboard", level="DEBUG") as debug_logs:
            simplefin._log_sync_retry_queue_state("test", account_state, routine_ranges)
        debug_text = "\n".join(debug_logs.output)
        self.assertNotIn("private-account", debug_text)
        self.assertIn('routine_retry_error_counts={"con.auth": 1}', debug_text)
        self.assertIn("account_backfill_window_days={\"90\": 1}", debug_text)

    def test_legacy_90_day_backfill_queue_is_clamped_and_made_eligible(self):
        now = int(simplefin._utc_now().timestamp())
        saved = {
            "account-private": {
                "start": now - 365 * 86400,
                "cursor": now - 365 * 86400,
                "end": now - 86400,
                "window_days": 90,
                "retry_after": now + 3600,
            }
        }

        with patch.object(simplefin, "get_setting", return_value=json.dumps(saved)), self.assertLogs(
            "finance_dashboard", level="INFO"
        ) as captured:
            state = simplefin._load_account_backfill_state()

        self.assertEqual(state["account-private"]["window_days"], 45)
        self.assertEqual(state["account-private"]["retry_after"], 0)
        log_text = "\n".join(captured.output)
        self.assertIn("clamped_window_accounts=1 window_limit_days=45 retry_after_cleared=true", log_text)
        self.assertNotIn("account-private", log_text)

    def test_gen_api_backfill_error_halves_the_window_and_retries_immediately(self):
        now = int(simplefin._utc_now().timestamp())
        day = simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        start = now - 200 * day
        end = start + 45 * day
        seed_latest = now - simplefin.SYNC_WINDOW_SECONDS
        self.seed_latest_transaction(seed_latest)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(now))
        simplefin._save_account_backfill_state({
            "account-1": {
                "start": start,
                "cursor": start,
                "end": end,
                "window_days": 45,
                "retry_after": 0,
            }
        })
        calls = []

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None, account_ids=None):
            calls.append((start_ts, end_ts))
            payload = self.payload([])
            if start_ts == start and end_ts == end:
                payload["errlist"] = [{
                    "code": "gen.api",
                    "msg": "Requested date range exceeds recommended range.",
                }]
            return payload

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=fetch_window), self.assertLogs(
            "finance_dashboard", level="INFO"
        ) as captured:
            result = simplefin.fetch_and_sync_simplefin()

        history_calls = [call for call in calls if call[0] >= start and call[1] <= end]
        self.assertEqual(history_calls, [
            (start, end),
            (start, start + 22 * day),
            (start + 22 * day, end),
        ])
        self.assertEqual(result["account_backfill_windows"], 3)
        self.assertFalse(result["account_backfill_pending"])
        self.assertEqual(simplefin._load_account_backfill_state(), {})
        log_text = "\n".join(captured.output)
        self.assertIn("adaptive_window_retry_accounts=1", log_text)
        self.assertIn("adaptive_window_days=45->22", log_text)
        self.assertIn('error_counts={"gen.api": 1}', log_text)

    def test_history_audit_reports_window_counts_without_persisting_data_or_coverage(self):
        now = int(simplefin._utc_now().timestamp())
        day = simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        start = now - 200 * day
        first_end = start + 45 * day
        second_end = first_end + 45 * day
        self.seed_latest_transaction(now - simplefin.SYNC_WINDOW_SECONDS)
        db = SessionLocal()
        try:
            db.add_all([
                AccountDataCoverage(
                    account_id="account-1",
                    start_ts=start,
                    end_ts=first_end,
                    status="complete",
                    source="account_backfill",
                    attempts=1,
                ),
                AccountDataCoverage(
                    account_id="account-1",
                    start_ts=first_end,
                    end_ts=second_end,
                    status="complete",
                    source="account_backfill",
                    attempts=1,
                ),
            ])
            db.commit()
        finally:
            db.close()

        first_payload = self.payload([
            {
                "id": "inside-window",
                "date": start + 1,
                "amount": "-1.00",
                "payee": "Private merchant",
                "description": "Private description",
            },
            {
                "id": "outside-window",
                "date": first_end + 1,
                "amount": "-2.00",
                "payee": "Another private merchant",
                "description": "Another private description",
            },
        ])
        fetch = Mock(side_effect=[first_payload, self.payload([])])
        budget = {
            "requests_last_24h": 4,
            "limit_24h": simplefin.SIMPLEFIN_MAX_REQUESTS_PER_24_HOURS,
            "requests_remaining": 20,
            "next_request_at": None,
        }

        with patch.object(simplefin, "_request_budget_status", return_value=budget), patch.object(
            simplefin, "_fetch_simplefin_payload", fetch
        ), self.assertLogs("finance_dashboard", level="INFO") as captured:
            result = simplefin.audit_account_history()

        self.assertTrue(result["ok"])
        self.assertEqual(result["windows_audited"], 2)
        self.assertEqual(result["transaction_rows"], 2)
        self.assertEqual(result["windows"][0]["transactions_in_requested_window"], 1)
        self.assertEqual(result["windows"][0]["transactions_outside_requested_window"], 1)
        self.assertEqual(result["windows"][1]["transaction_rows"], 0)
        self.assertEqual(fetch.call_args_list[0].args[1], start)
        self.assertEqual(fetch.call_args_list[1].args[1], first_end)
        log_text = "\n".join(captured.output)
        self.assertNotIn("Private merchant", log_text)
        self.assertNotIn("Private description", log_text)

        db = SessionLocal()
        try:
            self.assertEqual(db.query(Transaction).count(), 1)
            rows = db.query(AccountDataCoverage).filter_by(source="account_backfill").all()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row.status == "complete" and row.attempts == 1 for row in rows))
        finally:
            db.close()

    def test_history_audit_defers_if_request_budget_cannot_cover_all_windows(self):
        now = int(simplefin._utc_now().timestamp())
        start = now - 200 * simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        db = SessionLocal()
        try:
            db.add(AccountDataCoverage(
                account_id="account-1",
                start_ts=start,
                end_ts=start + simplefin.ACCOUNT_BACKFILL_WINDOW_SECONDS,
                status="complete",
                source="account_backfill",
                attempts=1,
            ))
            db.commit()
        finally:
            db.close()
        self.seed_latest_transaction(now - simplefin.SYNC_WINDOW_SECONDS)
        budget = {
            "requests_last_24h": 24,
            "limit_24h": simplefin.SIMPLEFIN_MAX_REQUESTS_PER_24_HOURS,
            "requests_remaining": 0,
            "next_request_at": None,
        }

        with patch.object(simplefin, "_request_budget_status", return_value=budget), patch.object(
            simplefin, "_fetch_simplefin_payload"
        ) as fetch:
            result = simplefin.audit_account_history()

        self.assertFalse(result["ok"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["windows_required"], 1)
        fetch.assert_not_called()

    def test_finance_stdin_dispatches_history_audit_command(self):
        audit_result = {
            "ok": True,
            "command": "audit_history",
            "windows_audited": 1,
            "windows_total": 1,
        }
        with patch.object(simplefin, "audit_account_history", return_value=audit_result) as audit:
            result = main._finance_agent_stdin_result({"command": "audit_history"})

        audit.assert_called_once_with()
        self.assertEqual(result, audit_result)

    def test_general_and_account_errors_keep_their_documented_scope(self):
        accounts = [
            {"id": "account-1", "conn_id": "connection-a"},
            {"id": "account-2", "conn_id": "connection-b"},
        ]
        global_errors = simplefin._coverage_errors(
            {"errlist": [{"code": "gen.auth"}], "accounts": accounts},
            {"account-1", "account-2"},
        )
        account_errors = simplefin._coverage_errors(
            {
                "errlist": [{"code": "act.failed", "account_id": "account-2"}],
                "accounts": accounts,
            },
            {"account-1", "account-2"},
        )

        self.assertEqual(global_errors, {"account-1": "gen.auth", "account-2": "gen.auth"})
        self.assertEqual(account_errors, {"account-2": "act.failed"})

    def test_unmatched_connection_error_fails_closed_without_logging_identifiers(self):
        payload = {
            "errlist": [{"code": "con.auth", "conn_id": "connection-private-unmatched"}],
            "accounts": [
                {"id": "account-private-a"},
                {"id": "account-private-b"},
            ],
        }
        requested = {"account-private-a", "account-private-b"}

        with self.assertLogs("finance_dashboard", level="INFO") as info_logs:
            errors = simplefin._coverage_errors(payload, requested)
        self.assertEqual(errors, {
            "account-private-a": "connection_error_unmatched",
            "account-private-b": "connection_error_unmatched",
        })
        self.assertNotIn("account-private", "\n".join(info_logs.output))
        self.assertNotIn("connection-private", "\n".join(info_logs.output))

        with self.assertLogs("finance_dashboard", level="DEBUG") as debug_logs:
            simplefin._coverage_errors(payload, requested)
        debug_text = "\n".join(debug_logs.output)
        self.assertNotIn("connection-private", debug_text)
        self.assertNotIn("account-private", debug_text)

    def test_auth_errors_are_not_added_to_automatic_routine_retry_queue(self):
        ranges = []
        queued = simplefin._queue_routine_retry_ranges(
            ranges,
            {"reauth-account": "con.auth"},
            1_700_000_000,
            1_700_086_400,
            {},
        )

        self.assertEqual(queued, 0)
        self.assertEqual(ranges, [])

    def test_unmatched_connection_error_does_not_count_ranges_as_verified(self):
        now = int(simplefin._utc_now().timestamp())
        start = now - simplefin.ACCOUNT_BACKFILL_DAYS * simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        account_ids = {"account-a", "account-b"}
        db = SessionLocal()
        try:
            db.add_all([
                Account(id="account-a", org="Test Bank", name="Checking A", balance="0", currency="USD"),
                Account(id="account-b", org="Test Bank", name="Checking B", balance="0", currency="USD"),
            ])
            db.commit()
        finally:
            db.close()
        payload = {
            "errlist": [{"code": "con.auth", "conn_id": "connection-not-in-response"}],
            "accounts": [{"id": account_id} for account_id in account_ids],
        }
        errors = simplefin._coverage_errors(payload, account_ids)
        simplefin._coverage_attempt(account_ids, start, now, "test")
        simplefin._coverage_result(
            account_ids,
            start,
            now,
            returned_ids=account_ids,
            error_codes=errors,
            source="test",
        )

        history = simplefin.get_account_data_history()["accounts"]
        self.assertEqual({item["account_id"] for item in history}, account_ids)
        self.assertTrue(all(item["coverage_percent"] == 0 for item in history))
        self.assertTrue(all(item["latest_error_code"] == "connection_error_unmatched" for item in history))

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

        dashboard_status = simplefin.get_dashboard_status()
        self.assertEqual(dashboard_status["incoming_transaction_count"], 1)
        self.assertEqual(dashboard_status["latest_incoming_transaction_date"], "2023-11-15")
        self.assertTrue(dashboard_status["newest_transaction_date_advanced"])

    def test_sync_reports_empty_provider_response(self):
        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(result["incoming_transaction_count"], 0)
        self.assertIsNone(result["latest_incoming_transaction_date"])
        self.assertEqual(result["account_backfill_windows"], 9)
        self.assertIn("Freshness NOT verified: SimpleFIN returned no transactions", result["message"])

    def test_sync_requests_exactly_one_day(self):
        now_timestamp = int(simplefin.datetime.now(simplefin.timezone.utc).timestamp())
        self.seed_latest_transaction(now_timestamp - simplefin.SYNC_WINDOW_SECONDS)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(now_timestamp))

        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])) as fetch:
            simplefin.fetch_and_sync_simplefin()

        start_timestamp = fetch.call_args.kwargs["start_ts"]
        end_timestamp = fetch.call_args.kwargs["end_ts"]
        self.assertEqual(end_timestamp - start_timestamp, 24 * 60 * 60)

    def test_new_accounts_get_12_months_in_45_day_windows(self):
        day = simplefin.SYNC_WINDOW_SECONDS
        account_ids = ("new-checking", "new-savings")

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None):
            self.assertLessEqual(end_ts - start_ts, simplefin.ACCOUNT_BACKFILL_WINDOW_SECONDS)
            if on_reserved:
                on_reserved()
            return self.new_account_payload(start_ts, account_ids)

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=fetch_window) as fetch:
            result = simplefin.fetch_and_sync_simplefin()

        calls = fetch.call_args_list
        self.assertEqual(len(calls), 10)
        self.assertEqual(result["account_backfill_windows"], 9)
        self.assertEqual(result["sync_windows"], 10)
        self.assertFalse(result["account_backfill_pending"])
        self.assertEqual(result["account_backfill_remaining_days"], 0)
        self.assertEqual(get_setting(simplefin.ACCOUNT_BACKFILL_SETTING_KEY), "{}")

        rolling_call = calls[0].kwargs
        account_backfill_calls = [call.kwargs for call in calls[1:]]
        self.assertEqual(account_backfill_calls[0]["start_ts"], rolling_call["start_ts"] - 364 * day)
        self.assertEqual(account_backfill_calls[-1]["end_ts"], rolling_call["start_ts"])
        for previous, following in zip(account_backfill_calls, account_backfill_calls[1:]):
            self.assertEqual(previous["end_ts"], following["start_ts"])

        db = SessionLocal()
        try:
            self.assertEqual(db.query(Account).count(), len(account_ids))
            self.assertEqual(db.query(Transaction).count(), 10 * len(account_ids))
        finally:
            db.close()

    def test_existing_accounts_get_one_time_12_month_backfill(self):
        now_timestamp = int(simplefin.datetime.now(simplefin.timezone.utc).timestamp())
        self.seed_latest_transaction(now_timestamp - simplefin.SYNC_WINDOW_SECONDS, history_migrated=False)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(now_timestamp))

        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])) as fetch:
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(fetch.call_count, 10)
        self.assertEqual(result["account_backfill_windows"], 9)
        self.assertFalse(result["account_backfill_pending"])

        with patch.object(simplefin, "_fetch_simplefin_payload", return_value=self.payload([])) as next_fetch:
            next_result = simplefin.fetch_and_sync_simplefin()
        self.assertEqual(next_fetch.call_count, 1)
        self.assertEqual(next_result["account_backfill_windows"], 0)

        coverage = simplefin.get_account_data_history()["accounts"]
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["coverage_percent"], 100.0)
        self.assertEqual(coverage[0]["gaps"], [])
        self.assertFalse(coverage[0]["stale"])

    def test_new_account_backfill_progress_resumes_after_request_failure(self):
        calls = 0

        def fail_after_three_backfill_windows(_access_url, start_ts, end_ts, *, on_reserved=None):
            nonlocal calls
            calls += 1
            if on_reserved:
                on_reserved()
            if calls == 5:
                raise RuntimeError("simulated SimpleFIN interruption")
            return self.new_account_payload(start_ts)

        with patch.object(
            simplefin,
            "_fetch_simplefin_payload",
            side_effect=fail_after_three_backfill_windows,
        ):
            failed_result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(failed_result["status"], "error")
        self.assertIn("progress is saved", failed_result["message"])
        saved_state = simplefin._load_account_backfill_state()
        self.assertIn("new-account", saved_state)
        saved_cursor = saved_state["new-account"]["cursor"]

        with patch.object(
            simplefin,
            "_fetch_simplefin_payload",
            side_effect=lambda _access_url, start_ts, end_ts, **kwargs: self.new_account_payload(start_ts),
        ) as fetch:
            resumed_result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(resumed_result["status"], "success")
        self.assertEqual(resumed_result["account_backfill_windows"], 6)
        self.assertFalse(resumed_result["account_backfill_pending"])
        self.assertEqual(get_setting(simplefin.ACCOUNT_BACKFILL_SETTING_KEY), "{}")
        resumed_windows = [call.kwargs for call in fetch.call_args_list[1:]]
        self.assertEqual(resumed_windows[0]["start_ts"], saved_cursor)

    def test_account_history_auth_failure_does_not_stall_healthy_accounts(self):
        now = int(simplefin._utc_now().timestamp())
        day = simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        start = now - simplefin.ACCOUNT_BACKFILL_DAYS * day
        end = now - simplefin.SYNC_WINDOW_SECONDS
        window_end = min(start + simplefin.ACCOUNT_BACKFILL_WINDOW_SECONDS, end)
        account_ids = ("healthy-account", "reauth-account")
        db = SessionLocal()
        try:
            db.add_all([
                Account(id="healthy-account", org="Test Bank", name="Healthy", balance="0", currency="USD"),
                Account(id="reauth-account", org="Test Bank", name="Needs reauth", balance="0", currency="USD"),
            ])
            db.commit()
        finally:
            db.close()
        set_setting(simplefin.ACCOUNT_BACKFILL_VERSION_KEY, simplefin.ACCOUNT_BACKFILL_VERSION)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(now))
        simplefin._save_account_backfill_state({
            account_id: {
                "start": start,
                "cursor": start,
                "end": end,
                "window_days": simplefin.ACCOUNT_BACKFILL_WINDOW_DAYS,
                "retry_after": 0,
            }
            for account_id in account_ids
        })

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None, account_ids=None):
            accounts = [
                {
                    "id": "healthy-account",
                    "conn_id": "MX-MBR-healthy",
                    "org": {"name": "Test Bank"},
                    "name": "Healthy",
                    "balance": "0",
                    "currency": "USD",
                    "transactions": [],
                },
                {
                    "id": "reauth-account",
                    "conn_id": "MX-MBR-reauth",
                    "org": {"name": "Test Bank"},
                    "name": "Needs reauth",
                    "balance": "0",
                    "currency": "USD",
                    "transactions": [],
                },
            ]
            payload = {"accounts": accounts}
            if start_ts == start:
                payload["errlist"] = [{"code": "con.auth", "conn_id": "MBR-reauth"}]
            return payload

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=fetch_window), self.assertLogs(
            "finance_dashboard", level="INFO"
        ) as captured:
            result = simplefin.fetch_and_sync_simplefin()

        saved_state = simplefin._load_account_backfill_state()
        self.assertNotIn("reauth-account", saved_state)
        self.assertEqual(saved_state, {})
        self.assertEqual(result["auth_failed_ranges"], 1)
        self.assertFalse(result["account_backfill_pending"])
        self.assertEqual(result["account_backfill_windows"], 9)
        self.assertFalse(simplefin._load_routine_retry_ranges())
        log_text = "\n".join(captured.output)
        self.assertIn("authentication_blocked_accounts=1", log_text)
        self.assertIn("account_history_pending=False", log_text)
        self.assertIn("auth_failed_ranges=1", log_text)
        self.assertNotIn("reauth-account", log_text)

        db = SessionLocal()
        try:
            coverage = {
                row.account_id: row.status
                for row in db.query(AccountDataCoverage).filter_by(start_ts=start, end_ts=window_end).all()
            }
        finally:
            db.close()
        self.assertEqual(coverage, {"healthy-account": "complete", "reauth-account": "failed"})

    def test_existing_routine_auth_retry_is_removed_after_auth_failure(self):
        now = int(simplefin._utc_now().timestamp())
        start = now - 3 * simplefin.SYNC_WINDOW_SECONDS
        end = start + simplefin.SYNC_WINDOW_SECONDS
        db = SessionLocal()
        try:
            db.add(Account(id="reauth-account", org="Test Bank", name="Needs reauth", balance="0", currency="USD"))
            db.commit()
        finally:
            db.close()
        set_setting(simplefin.ACCOUNT_BACKFILL_VERSION_KEY, simplefin.ACCOUNT_BACKFILL_VERSION)
        set_setting(simplefin.SYNC_CURSOR_SETTING_KEY, str(now))
        simplefin._save_routine_retry_ranges([{
            "account_id": "reauth-account",
            "start": start,
            "end": end,
            "retry_after": 0,
            "attempts": 1,
            "error_code": "con.auth",
        }])

        def fetch_window(_access_url, start_ts, end_ts, *, on_reserved=None, account_ids=None):
            payload = {"accounts": [{
                "id": "reauth-account",
                "conn_id": "MX-MBR-reauth",
                "org": {"name": "Test Bank"},
                "name": "Needs reauth",
                "balance": "0",
                "currency": "USD",
                "transactions": [],
            }]}
            if account_ids is not None:
                payload["errlist"] = [{"code": "con.auth", "conn_id": "MBR-reauth"}]
            return payload

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=fetch_window):
            result = simplefin.fetch_and_sync_simplefin()

        self.assertFalse(result["routine_retry_pending"])
        self.assertEqual(result["routine_retry_ranges_pending"], 0)
        self.assertEqual(result["auth_failed_ranges"], 1)
        self.assertEqual(simplefin._load_routine_retry_ranges(), [])
        db = SessionLocal()
        try:
            row = db.query(AccountDataCoverage).filter_by(start_ts=start, end_ts=end).one()
        finally:
            db.close()
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.error_code, "con.auth")

    def test_missing_data_error_splits_range_and_only_verified_children_count_as_coverage(self):
        calls = 0

        def incomplete_first_history_window(_access_url, start_ts, end_ts, *, on_reserved=None):
            nonlocal calls
            calls += 1
            if on_reserved:
                on_reserved()
            payload = self.new_account_payload(start_ts)
            if calls == 2:
                payload["errlist"] = [{
                    "code": "act.missingdata",
                    "account_id": "new-account",
                    "msg": "incomplete batch example",
                }]
            return payload

        with patch.object(simplefin, "_fetch_simplefin_payload", side_effect=incomplete_first_history_window) as fetch:
            result = simplefin.fetch_and_sync_simplefin()

        self.assertEqual(result["status"], "success")
        self.assertFalse(result["account_backfill_pending"])
        self.assertIn("incomplete transaction listing", result["message"])
        history = simplefin.get_account_data_history()["accounts"]
        self.assertEqual(history[0]["coverage_percent"], 100.0)
        self.assertEqual(history[0]["gaps"], [])
        self.assertGreater(fetch.call_count, 6)

    def test_rolling_request_budget_is_persisted_and_caps_at_documented_limit(self):
        set_setting(simplefin.SIMPLEFIN_REQUEST_BUDGET_KEY, "")
        simplefin._initialize_request_budget(simplefin.get_simplefin_token())
        with patch.object(simplefin, "SIMPLEFIN_MIN_REQUEST_INTERVAL_SECONDS", 0):
            for _ in range(simplefin.SIMPLEFIN_MAX_REQUESTS_PER_24_HOURS):
                simplefin._reserve_simplefin_request(simplefin.get_simplefin_token())
            with self.assertRaises(simplefin.SimpleFINDailyLimitError):
                simplefin._reserve_simplefin_request(simplefin.get_simplefin_token())
        self.assertEqual(simplefin._request_budget_status(simplefin.get_simplefin_token())["requests_last_24h"], 24)

    def test_legacy_daily_budget_is_migrated_without_losing_usage(self):
        access_url = simplefin.get_simplefin_token()
        now = simplefin._utc_now()
        set_setting(simplefin.SIMPLEFIN_REQUEST_BUDGET_KEY, json.dumps({
            "access_url_sha256": simplefin._access_url_fingerprint(access_url),
            "utc_date": now.date().isoformat(),
            "requests_today": 8,
            "last_request_at": now.timestamp() - 60,
            "unknown_usage": False,
        }))
        budget = simplefin._request_budget_status(access_url)
        self.assertEqual(budget["requests_last_24h"], 8)
        self.assertEqual(budget["requests_remaining"], 16)
        migrated = json.loads(get_setting(simplefin.SIMPLEFIN_REQUEST_BUDGET_KEY))
        self.assertEqual(len(migrated["request_timestamps"]), 8)

    def test_unknown_legacy_request_count_fails_closed_for_24_hours(self):
        set_setting(simplefin.SIMPLEFIN_REQUEST_BUDGET_KEY, "")
        set_setting("last_sync_at", simplefin._utc_now().isoformat())
        access_url = simplefin.get_simplefin_token()
        with self.assertRaises(simplefin.SimpleFINDailyLimitError):
            simplefin._reserve_simplefin_request(access_url)

        tomorrow = simplefin._utc_now() + simplefin.timedelta(days=1)
        with patch.object(simplefin, "_utc_now", return_value=tomorrow), patch.object(simplefin, "SIMPLEFIN_MIN_REQUEST_INTERVAL_SECONDS", 0):
            budget = simplefin._reserve_simplefin_request(access_url)
        self.assertEqual(budget["requests_last_24h"], 1)

    def test_coverage_uses_successful_responses_not_transaction_activity(self):
        now = int(simplefin._utc_now().timestamp())
        start = now - simplefin.ACCOUNT_BACKFILL_DAYS * simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        simplefin.save_simplefin_data(self.payload([]))
        simplefin._coverage_attempt(["account-1"], start, now, "test")
        simplefin._coverage_result(["account-1"], start, now, returned_ids=["account-1"], source="test")

        account = simplefin.get_account_data_history()["accounts"][0]
        self.assertEqual(account["coverage_percent"], 100.0)
        self.assertEqual(account["gaps"], [])
        self.assertFalse(account["stale"])

    def test_staleness_uses_last_successful_account_response(self):
        now = int(simplefin._utc_now().timestamp()) - (49 * 60 * 60)
        start = now - simplefin.ACCOUNT_BACKFILL_DAYS * simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        simplefin.save_simplefin_data(self.payload([]))
        simplefin._coverage_attempt(["account-1"], start, now, "test")
        simplefin._coverage_result(["account-1"], start, now, returned_ids=["account-1"], source="test")
        db = SessionLocal()
        try:
            row = db.query(AccountDataCoverage).filter_by(account_id="account-1", start_ts=start, end_ts=now).one()
            row.last_success_at = simplefin._coverage_time(now)
            db.commit()
        finally:
            db.close()

        report = simplefin.get_account_data_history()["accounts"][0]
        self.assertEqual(report["coverage_percent"], 100.0)
        self.assertTrue(report["stale"])
        self.assertGreaterEqual(report["hours_since_last_received"], 49)

    def test_history_page_can_queue_an_exact_missing_gap(self):
        now = int(simplefin._utc_now().timestamp())
        day = simplefin._ACCOUNT_BACKFILL_DAY_SECONDS
        start = now - simplefin.ACCOUNT_BACKFILL_DAYS * day
        gap_start = start + 100 * day
        gap_end = gap_start + 10 * day
        self.seed_latest_transaction(now - day)
        for left, right in ((start, gap_start), (gap_end, now)):
            simplefin._coverage_attempt(["account-1"], left, right, "test")
            simplefin._coverage_result(["account-1"], left, right, returned_ids=["account-1"], source="test")
        simplefin._coverage_attempt(["account-1"], gap_start, gap_end, "test")
        simplefin._coverage_result(
            ["account-1"], gap_start, gap_end,
            returned_ids=["account-1"],
            error_codes={"account-1": "act.missingdata"},
            source="test",
        )

        report = simplefin.get_account_data_history()["accounts"][0]
        gap = next(item for item in report["gaps"] if item["start_ts"] == gap_start)
        queued = simplefin.queue_account_history_retry("account-1", gap["start_ts"], gap["end_ts"])
        self.assertEqual(queued["status"], "queued")
        self.assertEqual(simplefin._load_account_backfill_state()["account-1"]["cursor"], gap_start)

    def test_sync_backfills_missed_days_with_contiguous_one_day_windows(self):
        now_timestamp = int(simplefin.datetime.now(simplefin.timezone.utc).timestamp())
        cursor = now_timestamp - (3 * simplefin.SYNC_WINDOW_SECONDS) - 3600
        self.seed_latest_transaction(cursor)
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
        self.seed_latest_transaction(cursor)
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

    def test_startup_returns_while_initial_sync_runs_in_background(self):
        started = threading.Event()
        release = threading.Event()

        def slow_startup_sync():
            started.set()
            release.wait(timeout=2)

        with patch.object(main, "init_db"), patch.object(main, "apply_app_log_level"), \
                patch.object(main, "_start_periodic_sync"), patch.object(main, "_run_startup_sync", side_effect=slow_startup_sync):
            started_at = time.monotonic()
            main.startup_event()
            elapsed = time.monotonic() - started_at
            self.assertTrue(started.wait(timeout=1))
            self.assertLess(elapsed, 0.5)
            release.set()

        if main._startup_sync_thread:
            main._startup_sync_thread.join(timeout=1)

    def test_only_one_simplefin_sync_runs_at_a_time(self):
        with main._simplefin_sync_lock:
            with patch.object(simplefin, "fetch_and_sync_simplefin") as fetch:
                result = main._serialized_simplefin_sync("test")

        self.assertEqual(result["status"], "in_progress")
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
