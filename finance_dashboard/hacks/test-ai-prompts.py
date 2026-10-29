#!/usr/bin/env python3
"""Tests encrypted, independently editable AI system prompt overrides."""
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
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-ai-prompts-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from ai_prompts import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPTS,
    MAX_SYSTEM_PROMPT_LENGTH,
    get_ai_prompt_settings,
    save_ai_system_prompt,
)
from database import init_db  # noqa: E402
from models import SessionLocal, Setting  # noqa: E402

init_db()


class AIPromptTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()

    def test_both_features_use_their_shipped_prompts_by_default(self):
        settings = get_ai_prompt_settings()
        self.assertEqual(set(settings["prompts"]), {"goal_planner", "category_rules"})
        self.assertIn("{{financial_snapshot}}", DEFAULT_SYSTEM_PROMPTS["goal_planner"])
        self.assertIn("{{category_analysis}}", DEFAULT_SYSTEM_PROMPTS["category_rules"])
        for prompt_id, default in DEFAULT_SYSTEM_PROMPTS.items():
            self.assertEqual(settings["prompts"][prompt_id]["prompt"], default)
            self.assertEqual(settings["prompts"][prompt_id]["default_prompt"], default)
            self.assertFalse(settings["prompts"][prompt_id]["is_custom"])
        self.assertEqual(settings["max_length"], MAX_SYSTEM_PROMPT_LENGTH)

    def test_custom_prompts_are_independent_and_encrypted_at_rest(self):
        custom = DEFAULT_SYSTEM_PROMPTS["goal_planner"] + "\nUse concise follow-up questions."
        saved = save_ai_system_prompt("goal_planner", custom)
        self.assertEqual(saved["prompt"], custom)
        self.assertTrue(saved["is_custom"])

        db = SessionLocal()
        try:
            stored = db.query(Setting).filter(Setting.key == "ai_prompt_goal_planner_override").one()
            self.assertNotEqual(stored._value, custom)
        finally:
            db.close()

        settings = get_ai_prompt_settings()["prompts"]
        self.assertEqual(settings["goal_planner"]["prompt"], custom)
        self.assertFalse(settings["category_rules"]["is_custom"])

    def test_reset_clears_only_that_prompt_override(self):
        save_ai_system_prompt("goal_planner", "Custom goals")
        save_ai_system_prompt("category_rules", "Custom categories")

        reset = save_ai_system_prompt("goal_planner", reset_to_default=True)
        self.assertEqual(reset["prompt"], DEFAULT_SYSTEM_PROMPTS["goal_planner"])
        self.assertFalse(reset["is_custom"])
        settings = get_ai_prompt_settings()["prompts"]
        self.assertTrue(settings["category_rules"]["is_custom"])
        self.assertEqual(settings["category_rules"]["prompt"], "Custom categories")

        category_reset = save_ai_system_prompt("category_rules", reset_to_default=True)
        self.assertEqual(category_reset["prompt"], DEFAULT_SYSTEM_PROMPTS["category_rules"])
        self.assertFalse(category_reset["is_custom"])

    def test_blank_oversized_and_unknown_prompts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be blank"):
            save_ai_system_prompt("goal_planner", "  \n ")
        with self.assertRaisesRegex(ValueError, "12000 characters"):
            save_ai_system_prompt("goal_planner", "x" * (MAX_SYSTEM_PROMPT_LENGTH + 1))
        with self.assertRaisesRegex(ValueError, "Unknown AI system prompt"):
            save_ai_system_prompt("unknown", "Custom")


if __name__ == "__main__":
    unittest.main(verbosity=2)
