#!/usr/bin/env python3
"""Tests the funding-account average and snowball payoff projection."""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-debt-payoff-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import get_setting, init_db, save_simplefin_data, set_setting
from debt_payoff import (
    DebtPayoffValidationError,
    SETTINGS_KEY,
    estimate_debt_terms_with_ai,
    estimate_minimum_payment,
    get_debt_payoff_plan,
    save_debt_payoff_settings,
)
from models import Account, SessionLocal, Setting, Transaction

init_db()


class DebtPayoffTests(unittest.TestCase):
    today = date(2026, 10, 30)

    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(Account).delete()
            db.query(Setting).filter(Setting.key == SETTINGS_KEY).delete()
            db.commit()
        finally:
            db.close()
        txs = []
        for month in range(5, 11):
            txs.append({
                "id": f"deposit-{month}",
                "date": int(datetime(2026, month, 5, tzinfo=timezone.utc).timestamp()),
                "amount": "600.00",
                "payee": "Test deposit",
                "description": "Test deposit",
                "pending": False,
            })
        txs.extend([
            {"id": "outflow", "date": int(datetime(2026, 8, 6, tzinfo=timezone.utc).timestamp()), "amount": "-100.00", "pending": False},
            {"id": "pending-inflow", "date": int(datetime(2026, 8, 7, tzinfo=timezone.utc).timestamp()), "amount": "500.00", "pending": True},
        ])
        save_simplefin_data({"accounts": [
            {"id": "staging", "name": "Card payment fund", "org": "Test Bank", "balance": "400.00", "currency": "USD", "account_type_suggestion": "bank_account", "transactions": txs},
            {"id": "high-card", "name": "High balance card", "org": "Issuer", "balance": "-5000.00", "currency": "USD", "account_type_suggestion": "credit_card", "transactions": []},
            {"id": "low-card", "name": "Low balance card", "org": "Issuer", "balance": "-1000.00", "currency": "USD", "account_type_suggestion": "credit_card", "transactions": []},
            {"id": "zero-card", "name": "Unused card", "org": "Issuer", "balance": "0.00", "currency": "USD", "account_type_suggestion": "credit_card", "transactions": []},
            {"id": "mortgage", "name": "Home loan", "org": "Test Lender", "balance": "-150000.00", "currency": "USD", "account_type_suggestion": "loan", "transactions": []},
            {"id": "euro-card", "name": "Euro card", "org": "Issuer", "balance": "-900.00", "currency": "EUR", "account_type_suggestion": "credit_card", "transactions": []},
        ]})
        self.accounts = [
            {"id": "staging", "name": "Card payment fund", "nickname": "", "balance": "400.00", "currency": "USD", "account_type": "bank_account"},
            {"id": "high-card", "name": "High balance card", "nickname": "", "balance": "-5000.00", "currency": "USD", "account_type": "credit_card"},
            {"id": "low-card", "name": "Low balance card", "nickname": "", "balance": "-1000.00", "currency": "USD", "account_type": "credit_card"},
            {"id": "zero-card", "name": "Unused card", "nickname": "", "balance": "0.00", "currency": "USD", "account_type": "credit_card"},
            {"id": "mortgage", "name": "Home loan", "nickname": "", "balance": "-150000.00", "currency": "USD", "account_type": "loan"},
            {"id": "euro-card", "name": "Euro card", "nickname": "", "balance": "-900.00", "currency": "EUR", "account_type": "credit_card"},
        ]
        self.history = {"accounts": [
            {"account_id": "staging", "stale": False, "gaps": []},
            {"account_id": "high-card", "stale": False, "gaps": []},
            {"account_id": "low-card", "stale": False, "gaps": []},
        ]}

    def settings_payload(self, **overrides):
        payload = {
            "funding_account_id": "staging",
            "current_target_account_id": "high-card",
            "strategy": "highest_balance",
            "monthly_budget_override": None,
            "included_account_ids": ["high-card", "low-card"],
            "debt_terms": [
                {"account_id": "high-card", "apr_percent": "0", "minimum_payment": "100"},
                {"account_id": "low-card", "apr_percent": "0", "minimum_payment": "50"},
            ],
            "manual_order": ["high-card", "low-card"],
        }
        payload.update(overrides)
        return payload

    def test_default_plan_hides_zero_balances_and_opts_out_of_mortgage(self):
        legacy = self.settings_payload()
        legacy.pop("included_account_ids")
        set_setting(SETTINGS_KEY, json.dumps(legacy))
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        listed_ids = {item["id"] for item in plan["liabilities"]}
        self.assertNotIn("zero-card", listed_ids)
        self.assertIn("mortgage", listed_ids)
        self.assertEqual(plan["settings"]["included_account_ids"], ["high-card", "low-card", "euro-card"])
        self.assertEqual(plan["total_balance"], "6000.00")
        self.assertNotIn("mortgage", {item["account_id"] for item in plan["debt_results"]})

    def test_other_debt_can_be_opted_in_and_requires_terms_only_then(self):
        payload = self.settings_payload(
            included_account_ids=["high-card", "low-card", "mortgage"],
            monthly_budget_override="2000",
            debt_terms=[
                {"account_id": "high-card", "apr_percent": "0", "minimum_payment": "100"},
                {"account_id": "low-card", "apr_percent": "0", "minimum_payment": "50"},
                {"account_id": "mortgage", "apr_percent": "6", "minimum_payment": "1200"},
            ],
        )
        save_debt_payoff_settings(payload, self.accounts)
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(plan["total_balance"], "156000.00")
        self.assertIn("mortgage", {item["account_id"] for item in plan["debt_results"]})

    def test_deposit_average_uses_posted_inflows_and_ignores_staging_balance(self):
        save_debt_payoff_settings(self.settings_payload(), self.accounts)
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(plan["deposit_average"]["total_deposits"], "3600.00")
        self.assertEqual(plan["deposit_average"]["monthly_average"], "600.00")
        self.assertEqual(plan["monthly_budget"], "600.00")
        self.assertNotEqual(plan["monthly_budget"], "1000.00")
        self.assertTrue(plan["deposit_average"]["history_complete"])

    def test_highest_balance_target_pays_other_minimums_and_projects_rollover(self):
        save_debt_payoff_settings(self.settings_payload(monthly_budget_override="1000"), self.accounts)
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(plan["status"], "projected")
        self.assertEqual(plan["effective_target_account_id"], "high-card")
        self.assertEqual(plan["payoff_order"][:2], ["high-card", "low-card"])
        self.assertEqual(plan["projected_months"], 6)
        self.assertEqual(plan["total_interest"], "0.00")
        self.assertEqual(plan["debt_paid"], "0.00")
        self.assertEqual(plan["progress_percent"], 0)

    def test_strategy_and_manual_order_are_respected(self):
        save_debt_payoff_settings(self.settings_payload(
            strategy="highest_apr",
            current_target_account_id="low-card",
            monthly_budget_override="1000",
            debt_terms=[
                {"account_id": "high-card", "apr_percent": "19.9", "minimum_payment": "100"},
                {"account_id": "low-card", "apr_percent": "24.9", "minimum_payment": "50"},
            ],
        ), self.accounts)
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(plan["payoff_order"][:2], ["low-card", "high-card"])
        self.assertEqual(plan["settings"]["strategy"], "highest_apr")

        save_debt_payoff_settings(self.settings_payload(
            strategy="manual",
            current_target_account_id="high-card",
            monthly_budget_override="1000",
            manual_order=["low-card", "high-card"],
        ), self.accounts)
        manual_plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(manual_plan["payoff_order"], ["high-card", "low-card"])

    def test_settings_for_another_currency_do_not_erase_saved_debt_terms(self):
        save_debt_payoff_settings(self.settings_payload(), self.accounts)
        save_debt_payoff_settings({
            "funding_account_id": "staging-eur",
            "current_target_account_id": "euro-card",
            "strategy": "highest_balance",
            "monthly_budget_override": None,
            "included_account_ids": ["euro-card"],
            "debt_terms": [{"account_id": "euro-card", "apr_percent": "12", "minimum_payment": "25"}],
            "manual_order": ["euro-card"],
        }, self.accounts + [
            {"id": "staging-eur", "name": "Euro fund", "balance": "10", "currency": "EUR", "account_type": "bank_account"},
        ])
        stored = get_setting(SETTINGS_KEY)
        self.assertIn('"high-card":{"apr_percent":"0.00","minimum_payment":"100.00"}', stored)
        self.assertIn('"euro-card":{"apr_percent":"12.00","minimum_payment":"25.00"}', stored)

    def test_reduced_balances_unlock_progress_milestones(self):
        save_debt_payoff_settings(self.settings_payload(), self.accounts)
        updated_accounts = [
            {**account, "balance": "-3000.00"} if account["id"] == "high-card" else account
            for account in self.accounts
        ]
        plan = get_debt_payoff_plan(updated_accounts, self.history, today=self.today)
        self.assertEqual(plan["debt_paid"], "2000.00")
        self.assertAlmostEqual(plan["progress_percent"], 33.3)
        self.assertTrue(plan["milestones"][0]["achieved"])
        self.assertFalse(plan["milestones"][1]["achieved"])

    def test_missing_terms_and_budget_below_minimums_are_not_given_a_payoff_date(self):
        save_debt_payoff_settings(self.settings_payload(
            monthly_budget_override="100",
            debt_terms=[{"account_id": "high-card", "apr_percent": "19.9", "minimum_payment": "100"}],
        ), self.accounts)
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(plan["status"], "needs_terms")
        self.assertIsNone(plan["projected_payoff_date"])

        save_debt_payoff_settings(self.settings_payload(monthly_budget_override="100"), self.accounts)
        plan = get_debt_payoff_plan(self.accounts, self.history, today=self.today)
        self.assertEqual(plan["status"], "below_minimums")
        self.assertIsNone(plan["projected_payoff_date"])

    def test_history_gaps_are_disclosed_even_though_a_deposit_average_is_returned(self):
        save_debt_payoff_settings(self.settings_payload(), self.accounts)
        history = {"accounts": [
            {"account_id": "staging", "stale": False, "gaps": [{
                "start_ts": int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()),
                "end_ts": int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp()),
            }]},
        ]}
        plan = get_debt_payoff_plan(self.accounts, history, today=self.today)
        self.assertFalse(plan["deposit_average"]["history_complete"])
        self.assertTrue(plan["history_warning"])

    def test_invalid_currency_target_and_manual_order_are_rejected(self):
        with self.assertRaisesRegex(DebtPayoffValidationError, "same currency"):
            save_debt_payoff_settings(self.settings_payload(current_target_account_id="euro-card"), self.accounts)
        with self.assertRaisesRegex(DebtPayoffValidationError, "unique included"):
            save_debt_payoff_settings(self.settings_payload(manual_order=["high-card", "high-card"]), self.accounts)
        with self.assertRaisesRegex(DebtPayoffValidationError, "greater than zero"):
            save_debt_payoff_settings(self.settings_payload(debt_terms=[
                {"account_id": "high-card", "apr_percent": "19.9", "minimum_payment": "0"},
                {"account_id": "low-card", "apr_percent": "22", "minimum_payment": "50"},
            ]), self.accounts)

    def test_local_minimum_formula_includes_interest_and_caps_at_amount_owed(self):
        self.assertEqual(estimate_minimum_payment("5000", "24"), Decimal("150.00"))
        self.assertEqual(estimate_minimum_payment("500", "0"), Decimal("25.00"))
        self.assertEqual(estimate_minimum_payment("10", "24"), Decimal("10.20"))

    def test_ai_request_contains_only_institution_name_and_calculates_minimum_locally(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"apr_percent":23.5,"note":"rough issuer-level estimate"}',
        ))])
        account = {
            "id": "private-account-id",
            "name": "My Personal Rewards Card",
            "org": "Test Issuer",
            "balance": "12345.67",
            "currency": "USD",
            "account_type": "credit_card",
        }
        with patch("debt_payoff.get_ai_settings", return_value={"enabled": True, "provider": "openai", "model": "test-model"}), \
             patch("debt_payoff._provider_key", return_value="test-key"), \
             patch("ai_chat._completion", return_value=response) as completion:
            estimate = estimate_debt_terms_with_ai(account)

        request_text = str(completion.call_args.args[2])
        self.assertIn("Test Issuer", request_text)
        self.assertNotIn("12345.67", request_text)
        self.assertNotIn("private-account-id", request_text)
        self.assertNotIn("My Personal Rewards Card", request_text)
        self.assertNotIn("USD", request_text)
        self.assertEqual(estimate["apr_percent"], "23.50")
        self.assertEqual(estimate["minimum_payment"], format(estimate_minimum_payment("12345.67", "23.5"), "f"))
        self.assertIn("unverified", estimate["note"].lower())

    def test_ai_estimate_uses_account_display_name_when_institution_is_not_distinct(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"apr_percent":21,"note":"rough estimate"}',
        ))])
        account = {
            "id": "private-account-id", "name": "Issuer-branded Card", "org": "Issuer-branded Card",
            "nickname": "Household Card", "balance": "500", "currency": "USD", "account_type": "credit_card",
        }
        with patch("debt_payoff.get_ai_settings", return_value={"enabled": True, "provider": "openai", "model": "test-model"}), \
             patch("debt_payoff._provider_key", return_value="test-key"), \
             patch("ai_chat._completion", return_value=response) as completion:
            estimate_debt_terms_with_ai(account)
        request_text = str(completion.call_args.args[2])
        self.assertIn("Household Card", request_text)
        self.assertNotIn("Issuer-branded Card", request_text)
        self.assertNotIn("private-account-id", request_text)
        self.assertNotIn("500", request_text)
        self.assertNotIn("USD", request_text)

    def test_settings_are_encrypted_in_the_existing_settings_store(self):
        save_debt_payoff_settings(self.settings_payload(), self.accounts)
        self.assertIn('"funding_account_id":"staging"', get_setting(SETTINGS_KEY))
        db = SessionLocal()
        try:
            raw = db.query(Setting).filter(Setting.key == SETTINGS_KEY).one()._value
            self.assertNotIn("staging", raw)
            self.assertNotIn("high-card", raw)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
