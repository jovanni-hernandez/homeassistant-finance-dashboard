#!/usr/bin/env python3
"""Tests planning assumptions and BLS CPI annualized inflation sync."""
from __future__ import annotations

import atexit
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-planning-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import planning  # noqa: E402
from database import init_db  # noqa: E402
from models import SessionLocal, Setting  # noqa: E402

init_db()


class PlanningSettingsTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(Setting).delete()
            db.commit()
        finally:
            db.close()

    def test_defaults_and_validated_updates_are_persisted(self):
        defaults = planning.get_planning_settings()
        self.assertEqual(defaults["inflation_window_years"], 10)
        self.assertEqual(defaults["withdrawal_rate_percent"], "4")

        saved = planning.save_planning_settings({
            "birth_year": 1989,
            "expected_return_percent": "6.5",
            "withdrawal_rate_percent": "3.8",
            "inflation_percent": "2.7",
            "inflation_source": "manual",
            "inflation_window_years": 5,
        })
        self.assertEqual(saved["birth_year"], 1989)
        self.assertEqual(saved["inflation_window_years"], 5)
        self.assertEqual(saved["inflation_source"], "manual")
        self.assertEqual(planning.get_planning_settings(), saved)

    def test_rejects_unsupported_window_and_invalid_rates(self):
        payload = dict(planning.DEFAULT_PLANNING_SETTINGS)
        payload["inflation_window_years"] = 7
        with self.assertRaisesRegex(planning.PlanningSettingsError, "5, 10, or 20"):
            planning.save_planning_settings(payload)
        payload["inflation_window_years"] = 10
        payload["withdrawal_rate_percent"] = "0"
        with self.assertRaisesRegex(planning.PlanningSettingsError, "Withdrawal rate"):
            planning.save_planning_settings(payload)

    def test_sync_uses_compounded_cpi_change_and_preserves_as_of_metadata(self):
        settings = planning.get_planning_settings()
        settings["inflation_window_years"] = 5
        planning.set_setting(planning.PLANNING_SETTINGS_KEY, json.dumps(settings))
        today = date.today()
        month = today.month
        observations = {
            (today.year, month): Decimal("110"),
            (today.year - 1, month): Decimal("108"),
            (today.year - 5, month): Decimal("100"),
        }
        with patch.object(planning, "_fetch_window_data", return_value=observations):
            synced = planning.sync_bls_inflation()

        self.assertAlmostEqual(float(synced["inflation_percent"]), (1.1 ** 0.2 - 1) * 100, places=2)
        self.assertEqual(synced["inflation_latest_yoy_percent"], "1.85")
        self.assertEqual(synced["inflation_source"], "bls")
        self.assertEqual(synced["inflation_as_of"], f"{today.year}-{month:02d}")
        self.assertTrue(synced["inflation_synced_at"])

    def test_twenty_year_window_is_split_into_bls_supported_ranges(self):
        calls = []

        def fake_fetch(start_year, end_year):
            calls.append((start_year, end_year))
            return [
                {"year": str(year), "period": "M01", "value": "100"}
                for year in range(start_year, end_year + 1)
            ]

        with patch.object(planning, "_fetch_bls_series", side_effect=fake_fetch):
            planning._fetch_window_data(20, 2026)
        self.assertEqual(calls, [(2006, 2015), (2016, 2025), (2026, 2026)])

    def test_failed_sync_does_not_overwrite_saved_settings(self):
        before = planning.save_planning_settings({
            "birth_year": None,
            "expected_return_percent": "6",
            "withdrawal_rate_percent": "4",
            "inflation_percent": "2.8",
            "inflation_source": "manual",
            "inflation_window_years": 10,
        })
        with patch.object(planning, "_fetch_window_data", return_value={}):
            with self.assertRaisesRegex(planning.PlanningSettingsError, "no usable"):
                planning.sync_bls_inflation()
        self.assertEqual(planning.get_planning_settings(), before)

        today = date.today()
        stale_year, stale_month = today.year, today.month - 5
        if stale_month <= 0:
            stale_year -= 1
            stale_month += 12
        stale_observations = {
            (stale_year, stale_month): Decimal("120"),
            (stale_year - 10, stale_month): Decimal("100"),
        }
        with patch.object(planning, "_fetch_window_data", return_value=stale_observations):
            with self.assertRaisesRegex(planning.PlanningSettingsError, "over four months old"):
                planning.sync_bls_inflation()
        self.assertEqual(planning.get_planning_settings(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
