#!/usr/bin/env python3
"""Unit checks for merchant normalization and recurring cadence gates."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import atexit
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="recurring-unit-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))

from recurring_analysis import _looks_recurring, merchant_key  # noqa: E402


class RecurringAnalysisTests(unittest.TestCase):
    def test_keeps_long_merchant_names_and_drops_numeric_references(self):
        self.assertEqual(merchant_key("Starbucks", ""), "starbucks")
        self.assertEqual(merchant_key("Microsoft 987654321", ""), "microsoft")

    def test_two_transactions_need_plausible_cycle_and_stable_amount(self):
        self.assertFalse(_looks_recurring(2, 3, 0, 0.0))
        self.assertTrue(_looks_recurring(2, 30, 0, 0.05))
        self.assertFalse(_looks_recurring(2, 30, 0, 0.5))

    def test_three_transactions_need_regular_intervals(self):
        self.assertTrue(_looks_recurring(3, 30, 2, 0.4))
        self.assertFalse(_looks_recurring(3, 30, 20, 0.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
