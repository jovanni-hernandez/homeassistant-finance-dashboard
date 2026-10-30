#!/usr/bin/env python3
"""Tests encrypted financial-goal persistence, validation, and estimates."""
from __future__ import annotations

import atexit
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-goals-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import get_setting, init_db, save_simplefin_data
from goals import GoalValidationError, create_goal, delete_goal, get_goals, update_goal
from models import Account, ManualAsset, SessionLocal, Setting, Transaction
from database import create_manual_asset

init_db()


def seed_sources():
    save_simplefin_data({"accounts": [
        {"id": "bank-usd", "org": "Test Bank", "name": "Savings", "balance": "1000.00", "currency": "USD", "account_type_suggestion": "bank_account", "transactions": []},
        {"id": "investment-usd", "org": "Test Broker", "name": "Retirement", "balance": "10000.00", "currency": "USD", "account_type_suggestion": "investment", "transactions": []},
        {"id": "credit-usd", "org": "Test Bank", "name": "Credit Card", "balance": "-12000.00", "currency": "USD", "account_type_suggestion": "credit_card", "transactions": []},
        {"id": "bank-eur", "org": "Test Bank", "name": "Euro Savings", "balance": "500.00", "currency": "EUR", "account_type_suggestion": "bank_account", "transactions": []},
    ]})
    return {
        "bank": {"id": "bank-usd", "name": "Savings", "balance": "1000.00", "currency": "USD", "account_type": "bank_account"},
        "investment": {"id": "investment-usd", "name": "Retirement", "balance": "10000.00", "currency": "USD", "account_type": "investment"},
        "debt": {"id": "credit-usd", "name": "Credit Card", "balance": "-12000.00", "currency": "USD", "account_type": "credit_card"},
        "euro_bank": {"id": "bank-eur", "name": "Euro Savings", "balance": "500.00", "currency": "EUR", "account_type": "bank_account"},
    }


class FinancialGoalTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(Account).delete()
            db.query(ManualAsset).delete()
            db.query(Setting).filter(Setting.key == "financial_goals").delete()
            db.commit()
        finally:
            db.close()
        self.sources = seed_sources()
        self.manual_investment = create_manual_asset("Retirement holding", "5000.00", "USD")

    def test_savings_goal_shows_progress_and_required_monthly_pace(self):
        goal = create_goal({
            "goal_type": "savings",
            "name": "Emergency fund",
            "account_ids": ["bank-usd"],
            "target_amount": "6000",
            "target_date": "2027-04-01",
            "planned_monthly": "900",
        }, [self.sources["bank"]], [])

        self.assertEqual(goal["currency"], "USD")
        self.assertEqual(goal["current_amount"], "1000.00")
        self.assertEqual(goal["remaining_amount"], "5000.00")
        self.assertEqual(goal["required_monthly"], "833.34")
        self.assertEqual(goal["status"], "on_track")

    def test_debt_payoff_calculates_payment_using_configured_apr(self):
        goal = create_goal({
            "goal_type": "debt_payoff",
            "name": "Pay off card",
            "account_ids": ["credit-usd"],
            "target_date": "2027-10-01",
            "apr_percent": "12",
            "planned_monthly": "1100",
        }, [self.sources["debt"]], [])

        self.assertEqual(goal["current_amount"], "12000.00")
        self.assertGreater(Decimal(goal["required_monthly"]), Decimal("1000"))
        self.assertEqual(goal["status"], "on_track")
        self.assertEqual(goal["currency"], "USD")

    def test_retirement_goal_accepts_birth_year_or_retirement_year_and_manual_assets(self):
        goal = create_goal({
            "goal_type": "retirement",
            "name": "Retirement",
            "account_ids": ["investment-usd"],
            "manual_asset_ids": [self.manual_investment["id"]],
            "target_amount": "100000",
            "retirement_timing_mode": "year",
            "retirement_year": 2030,
            "expected_return_percent": "5",
            "inflation_percent": "2",
            "planned_monthly": "500",
        }, [self.sources["investment"]], [self.manual_investment])

        self.assertEqual(goal["current_amount"], "15000.00")
        self.assertEqual(goal["retirement_year"], 2030)
        self.assertGreater(Decimal(goal["target_nominal_amount"]), Decimal("100000"))
        self.assertGreater(Decimal(goal["required_monthly"]), Decimal("0"))
        self.assertIn("Retirement holding", goal["source_names"])

        age_goal = create_goal({
            "goal_type": "retirement",
            "name": "Age-mode retirement",
            "account_ids": ["investment-usd"],
            "target_amount": "200000",
            "retirement_timing_mode": "age",
            "birth_year": 1980,
            "target_retirement_age": 65,
            "expected_return_percent": "0",
            "inflation_percent": "0",
        }, [self.sources["investment"]], [])
        self.assertEqual(age_goal["retirement_year"], 2045)

    def test_retirement_monthly_income_target_derives_today_dollar_corpus(self):
        goal = create_goal({
            "goal_type": "retirement",
            "name": "Income-based retirement",
            "account_ids": ["investment-usd"],
            "monthly_income_target": "10000",
            "withdrawal_rate_percent": "4",
            "retirement_timing_mode": "year",
            "retirement_year": 2045,
            "expected_return_percent": "6",
            "inflation_percent": "2.5",
        }, [self.sources["investment"]], [])

        self.assertEqual(goal["monthly_income_target"], "10000")
        self.assertEqual(goal["withdrawal_rate_percent"], "4")
        self.assertEqual(goal["target_amount"], "3000000.00")
        self.assertGreater(Decimal(goal["target_nominal_amount"]), Decimal("3000000"))

    def test_goal_sources_must_be_compatible_and_currency_matched(self):
        with self.assertRaisesRegex(GoalValidationError, "liability"):
            create_goal({
                "goal_type": "debt_payoff", "name": "Invalid debt", "account_ids": ["bank-usd"],
                "target_date": "2027-10-01", "apr_percent": "0",
            }, list(self.sources.values()), [])

        with self.assertRaisesRegex(GoalValidationError, "same currency"):
            create_goal({
                "goal_type": "savings", "name": "Mixed currency", "account_ids": ["bank-usd", "bank-eur"],
                "target_amount": "1000",
            }, [self.sources["bank"], self.sources["euro_bank"]], [])

    def test_goal_records_are_encrypted_and_update_delete_work(self):
        goal = create_goal({
            "goal_type": "savings", "name": "Private reserve", "account_ids": ["bank-usd"],
            "target_amount": "6000",
        }, [self.sources["bank"]], [])
        stored = get_setting("financial_goals")
        self.assertIn("Private reserve", stored)

        db = SessionLocal()
        try:
            raw_value = db.query(Setting).filter(Setting.key == "financial_goals").one()._value
            self.assertNotIn("Private reserve", raw_value)
            self.assertNotIn("6000", raw_value)
        finally:
            db.close()

        updated = update_goal(goal["id"], {
            "goal_type": "savings", "name": "Updated reserve", "account_ids": ["bank-usd"],
            "target_amount": "7000",
        }, [self.sources["bank"]], [])
        self.assertEqual(updated["name"], "Updated reserve")
        self.assertEqual(len(get_goals([self.sources["bank"]], [])), 1)
        self.assertTrue(delete_goal(goal["id"]))
        self.assertFalse(delete_goal(goal["id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
