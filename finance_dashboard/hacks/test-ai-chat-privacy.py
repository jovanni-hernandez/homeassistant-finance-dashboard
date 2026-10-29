#!/usr/bin/env python3
"""Checks aggregate-only AI chat context and local identifier redaction."""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-ai-chat-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import ai_chat  # noqa: E402
from ai_assistant import AI_ENABLED_KEY, AI_MODEL_KEY, AI_OPENAI_KEY, AI_PROVIDER_KEY  # noqa: E402
from database import (  # noqa: E402
    create_manual_asset,
    get_manual_assets,
    get_setting,
    get_stored_accounts,
    init_db,
    save_simplefin_data,
    set_setting,
)
from goals import create_goal, get_goals  # noqa: E402
from models import Account, ManualAsset, SessionLocal, Setting, Transaction  # noqa: E402

init_db()


class AIChatPrivacyTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Transaction).delete()
            db.query(Account).delete()
            db.query(ManualAsset).delete()
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()

        now = int(datetime.now(timezone.utc).timestamp())
        self.private_names = [
            "Primary Checking",
            "Example Bank",
            "Retirement Plan",
            "Example Broker",
            "Private Condo",
            "My Retirement Goal",
            "Coffee Merchant Example",
        ]
        save_simplefin_data({"accounts": [
            {
                "id": "checking-123456789",
                "org": {"name": "Example Bank"},
                "name": "Primary Checking",
                "balance": "4000.00",
                "currency": "USD",
                "account_type_suggestion": "bank_account",
                "transactions": [
                    {"id": "privacy-tx-1", "date": now, "amount": "500.00", "payee": "Payroll Example", "description": "Private payroll note", "category": "Income", "pending": False},
                    {"id": "privacy-tx-2", "date": now - 86400, "amount": "-125.50", "payee": "Coffee Merchant Example", "description": "Private cafe note", "category": "Dining", "pending": False},
                    {"id": "privacy-tx-3", "date": now - 172800, "amount": "-900.00", "payee": "Internal Transfer", "description": "Transfer between accounts", "category": "Transfers", "pending": False},
                ],
            },
            {
                "id": "retirement-987654321",
                "org": {"name": "Example Broker"},
                "name": "Retirement Plan",
                "balance": "100000.00",
                "currency": "USD",
                "account_type_suggestion": "investment",
                "transactions": [],
            },
        ]})
        self.manual_asset = create_manual_asset("Private Condo", "120000.00", "USD")
        create_goal({
            "goal_type": "retirement",
            "name": "My Retirement Goal",
            "account_ids": ["retirement-987654321"],
            "manual_asset_ids": [self.manual_asset["id"]],
            "target_amount": "500000",
            "retirement_timing_mode": "year",
            "retirement_year": datetime.now(timezone.utc).year + 20,
            "expected_return_percent": "5",
            "inflation_percent": "2",
        }, get_stored_accounts(), get_manual_assets())
        set_setting(AI_ENABLED_KEY, "true")
        set_setting(AI_PROVIDER_KEY, "openai")
        set_setting(AI_MODEL_KEY, "privacy-test-model")
        set_setting(AI_OPENAI_KEY, "fake-test-key")

    def test_context_contains_aggregates_but_no_names_ids_or_transaction_text(self):
        accounts = get_stored_accounts()
        assets = get_manual_assets()
        goals = get_goals(accounts, assets)
        transactions = ai_chat._recent_transactions(datetime.now(timezone.utc).date())
        context = ai_chat.build_sanitized_financial_context(accounts, assets, goals, transactions)
        payload = json.dumps(context)

        for private_value in self.private_names + ["checking-123456789", "retirement-987654321", "privacy-tx-1", "Private payroll note", "Private cafe note"]:
            self.assertNotIn(private_value, payload)
        self.assertIn("Dining", payload)
        self.assertIn("bank_account", payload)
        self.assertIn("125.5", payload)
        self.assertIn("4000.0", payload)
        self.assertEqual(len(context["monthly_activity"]), 1)
        self.assertNotIn("Transfers", payload)

    def test_preview_redacts_known_names_and_long_numbers_but_keeps_categories_and_amounts(self):
        preview = ai_chat.preview_chat_message(
            "Could Primary Checking at Example Bank use 123456789 toward Groceries? About $500 monthly, or $123456789 by 2030-12-31."
        )
        self.assertGreaterEqual(preview["redaction_count"], 3)
        self.assertNotIn("Primary Checking", preview["sanitized_message"])
        self.assertNotIn("Example Bank", preview["sanitized_message"])
        self.assertEqual(preview["sanitized_message"].count("123456789"), 1)
        self.assertIn("[redacted number]", preview["sanitized_message"])
        self.assertIn("Groceries", preview["sanitized_message"])
        self.assertIn("$500", preview["sanitized_message"])
        self.assertIn("$123456789", preview["sanitized_message"])
        self.assertIn("2030-12-31", preview["sanitized_message"])

    def test_provider_receives_only_sanitized_context_and_goal_is_draft_only(self):
        captured = {}

        def fake_completion(model, api_key, messages):
            captured.update({"model": model, "api_key": api_key, "messages": messages})
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=(
                "A savings target could be a useful next step.\n"
                '<goal_draft>{"goal_type":"savings","name":"Emergency Fund","target_amount":"10000","target_date":"2030-12-31"}</goal_draft>'
            )))])

        before = len(get_goals(get_stored_accounts(), get_manual_assets()))
        with patch.object(ai_chat, "_completion", side_effect=fake_completion):
            result = ai_chat.create_goal_chat_response([{
                "role": "user",
                "content": "Help me use Primary Checking 123456789 to build a Groceries emergency fund of $10000.",
            }])

        provider_payload = json.dumps(captured["messages"])
        for private_value in self.private_names + ["checking-123456789", "retirement-987654321", "Coffee Merchant Example", "Private cafe note"]:
            self.assertNotIn(private_value, provider_payload)
        self.assertIn("Groceries", provider_payload)
        self.assertIn("10000", provider_payload)
        self.assertEqual(captured["api_key"], "fake-test-key")
        self.assertEqual(result["goal_draft"]["goal_type"], "savings")
        self.assertNotIn("account_ids", result["goal_draft"])
        self.assertEqual(len(get_goals(get_stored_accounts(), get_manual_assets())), before)

    def test_disabled_ai_does_not_call_provider(self):
        set_setting(AI_ENABLED_KEY, "false")
        with patch.object(ai_chat, "_completion") as completion:
            with self.assertRaisesRegex(ai_chat.AIChatError, "Enable AI assistance"):
                ai_chat.create_goal_chat_response([{"role": "user", "content": "Help me set a goal."}])
        completion.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
