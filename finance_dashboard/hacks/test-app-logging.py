#!/usr/bin/env python3
"""Tests persisted log verbosity and debug-only full AI exchange logging."""
from __future__ import annotations

import atexit
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-app-logging-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app_logging import apply_app_log_level, get_app_log_settings  # noqa: E402
from ai_logging import log_ai_exchange  # noqa: E402
from database import init_db  # noqa: E402
from models import SessionLocal, Setting  # noqa: E402

init_db()


class AppLoggingTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()
        self.logger = logging.getLogger("finance_dashboard")
        self.original_level = self.logger.level
        apply_app_log_level("INFO")

    def tearDown(self):
        self.logger.setLevel(self.original_level)

    def test_info_is_default_and_debug_setting_applies_immediately(self):
        self.assertEqual(get_app_log_settings()["level"], "INFO")
        self.assertFalse(get_app_log_settings()["full_ai_payload_logging"])

        apply_app_log_level("DEBUG")
        self.assertEqual(get_app_log_settings()["level"], "DEBUG")
        self.assertTrue(get_app_log_settings()["full_ai_payload_logging"])
        self.assertEqual(self.logger.getEffectiveLevel(), logging.DEBUG)

    def test_invalid_log_level_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "INFO or DEBUG"):
            apply_app_log_level("TRACE")

    def test_debug_exchange_contains_details_but_redacts_configured_and_bearer_keys(self):
        apply_app_log_level("DEBUG")
        configured_key = "sk-proj-example-secret-value-123456"
        with self.assertLogs("finance_dashboard", level="DEBUG") as captured:
            log_ai_exchange(
                event="goal_provider_response",
                provider="openai",
                model="example-model",
                request={"messages": [{"role": "user", "content": "private financial question"}]},
                api_key=configured_key,
                response={"content": "example answer", "debug_token": "Bearer other-secret-token-123"},
                status=200,
            )

        output = "\n".join(captured.output)
        self.assertIn("private financial question", output)
        self.assertIn("example answer", output)
        self.assertNotIn(configured_key, output)
        self.assertNotIn("other-secret-token-123", output)
        self.assertIn("REDACTED_API_KEY", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
