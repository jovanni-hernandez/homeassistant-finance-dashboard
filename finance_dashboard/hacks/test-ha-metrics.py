#!/usr/bin/env python3
"""Check Home Assistant metrics aggregation and optional token behavior."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-ha-metrics-", dir=TEST_ROOT)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import init_db  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from ha_metrics import (  # noqa: E402
    MAX_EXPOSED_TRANSACTIONS,
    build_ha_metrics,
    metrics_token_matches,
    read_metrics_api_token,
)
from main import api_ha_metrics  # noqa: E402
from models import Account, ManualAsset, SessionLocal, Transaction  # noqa: E402

init_db()


class HomeAssistantMetricsTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(ManualAsset).delete()
            db.query(Account).delete()
            db.commit()

            db.add_all(
                [
                    Account(
                        id="cash-account",
                        name="Checking",
                        nickname="Daily",
                        balance="1000.00",
                        currency="USD",
                        account_type="bank_account",
                    ),
                    Account(
                        id="credit-account",
                        name="Card",
                        balance="100.00",
                        currency="USD",
                        account_type="credit_card",
                    ),
                    Account(
                        id="unclassified-account",
                        name="Unclassified",
                        balance="9000.00",
                        currency="USD",
                        account_type="unclassified",
                    ),
                ]
            )
            db.add(
                ManualAsset(
                    id="home-asset",
                    name="Home",
                    value="2500.00",
                    currency="USD",
                )
            )
            month_start = int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp())
            db.add_all(
                [
                    Transaction(
                        id="income",
                        account_id="cash-account",
                        date=month_start + 100,
                        amount="100.00",
                        payee="Employer",
                        description="Paycheck",
                        category="Income",
                        pending=False,
                    ),
                    Transaction(
                        id="expense",
                        account_id="cash-account",
                        date=month_start + 200,
                        amount="-30.00",
                        payee="Market",
                        description="Groceries",
                        category="Food",
                        pending=False,
                    ),
                    Transaction(
                        id="transfer",
                        account_id="cash-account",
                        date=month_start + 300,
                        amount="-50.00",
                        payee="Zelle",
                        description="Transfer",
                        category="Uncategorized",
                        pending=False,
                    ),
                    Transaction(
                        id="pending",
                        account_id="cash-account",
                        date=month_start + 400,
                        amount="-5.00",
                        payee="Pending shop",
                        description="Pending",
                        category="Shopping",
                        pending=True,
                    ),
                    Transaction(
                        id="manual-category-transfer-name",
                        account_id="cash-account",
                        date=month_start + 500,
                        amount="-10.00",
                        payee="Zelle payment",
                        description="Gift to family",
                        category="Gifts",
                        pending=False,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

    def test_aggregates_balance_and_cash_flow_with_dashboard_rules(self):
        metrics = build_ha_metrics(
            {
                "connected": True,
                "last_sync_status": "success",
                "last_sync_at": "2026-04-15T12:00:00+00:00",
                "incoming_transaction_count": 2,
                "latest_incoming_transaction_date": "2026-04-14",
                "newest_transaction_date_advanced": True,
                "account_count": 3,
                "transaction_count": 4,
            },
            now=datetime(2026, 4, 15, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(metrics["month"], "2026-04")
        self.assertEqual(metrics["status"]["account_count"], 3)
        self.assertEqual(metrics["status"]["incoming_transaction_count"], 2)
        self.assertEqual(metrics["status"]["latest_incoming_transaction_date"], "2026-04-14")
        self.assertTrue(metrics["status"]["newest_transaction_date_advanced"])
        self.assertEqual(metrics["net_worth"], [
            {"currency": "USD", "assets": 3500.0, "liabilities": 100.0, "net_worth": 3400.0}
        ])
        self.assertEqual(metrics["cash_flow"], [
            {"currency": "USD", "income": 100.0, "spending": 40.0, "net": 60.0, "transaction_count": 3}
        ])
        self.assertEqual(next(a for a in metrics["accounts"] if a["id"] == "cash-account")["nickname"], "Daily")
        self.assertNotIn("transactions", metrics)

    def test_raw_transactions_are_bounded_and_only_included_on_request(self):
        db = SessionLocal()
        try:
            date = int(datetime(2026, 4, 15, tzinfo=timezone.utc).timestamp())
            db.add_all(
                [
                    Transaction(
                        id=f"history-{index}",
                        account_id="cash-account",
                        date=date + index,
                        amount=str(index),
                        payee=f"Payee {index}",
                        description=f"Description {index}",
                        category="Shopping",
                        pending=False,
                    )
                    for index in range(MAX_EXPOSED_TRANSACTIONS + 5)
                ]
            )
            db.commit()
        finally:
            db.close()

        status = {"account_count": 3, "transaction_count": 25}
        without_transactions = build_ha_metrics(
            status,
            now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        )
        with_transactions = build_ha_metrics(
            status,
            include_transactions=True,
            now=datetime(2026, 4, 15, tzinfo=timezone.utc),
        )

        self.assertNotIn("transactions", without_transactions)
        self.assertEqual(len(with_transactions["transactions"]), MAX_EXPOSED_TRANSACTIONS)
        self.assertIn("description", with_transactions["transactions"][0])
        self.assertIn("account_name", with_transactions["transactions"][0])

    def test_token_is_optional_but_enforced_when_configured(self):
        self.assertTrue(metrics_token_matches("", None))
        self.assertTrue(metrics_token_matches("secret-token", "secret-token"))
        self.assertFalse(metrics_token_matches("secret-token", None))
        self.assertFalse(metrics_token_matches("secret-token", "wrong-token"))

        options_path = Path(TEST_DATA.name) / "options.json"
        options_path.write_text(json.dumps({"ha_metrics_api_token": "private-token"}), encoding="utf-8")
        self.assertEqual(read_metrics_api_token(str(options_path)), "private-token")

    def test_metrics_route_requires_token_only_when_one_is_configured(self):
        with (
            patch("main.read_metrics_api_token", return_value="private-token"),
            patch("main.simplefin.get_dashboard_status", return_value={"account_count": 0}),
            patch("main.build_ha_metrics", return_value={"accounts": []}) as build_metrics,
        ):
            with self.assertRaises(HTTPException) as error:
                api_ha_metrics(metrics_token=None)
            self.assertEqual(error.exception.status_code, 401)

            response = api_ha_metrics(metrics_token="private-token", include_transactions=True)
            self.assertEqual(response, {"accounts": []})
            build_metrics.assert_called_once_with(
                {"account_count": 0},
                include_transactions=True,
            )

        with (
            patch("main.read_metrics_api_token", return_value=""),
            patch("main.simplefin.get_dashboard_status", return_value={}),
            patch("main.build_ha_metrics", return_value={"status": {}}) as build_metrics,
        ):
            self.assertEqual(api_ha_metrics(metrics_token=None), {"status": {}})
            build_metrics.assert_called_once_with({}, include_transactions=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
