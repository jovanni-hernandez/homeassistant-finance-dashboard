#!/usr/bin/env python3
"""Checks manual-asset persistence, validation, and SimpleFIN isolation."""
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
TEST_DATA = tempfile.TemporaryDirectory(prefix="finance-manual-assets-", dir=TEST_ROOT)
atexit.register(TEST_DATA.cleanup)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import (  # noqa: E402
    create_manual_asset,
    delete_manual_asset,
    get_manual_assets,
    init_db,
    save_simplefin_data,
    update_manual_asset,
)
from models import ManualAsset, SessionLocal  # noqa: E402

init_db()


class ManualAssetTests(unittest.TestCase):
    def setUp(self):
        db = SessionLocal()
        try:
            db.query(ManualAsset).delete()
            db.commit()
        finally:
            db.close()

    def test_create_update_and_delete_asset(self):
        house = create_manual_asset("  Home  ", "425000.00", "usd")
        self.assertEqual(house["name"], "Home")
        self.assertEqual(house["value"], "425000.00")
        self.assertEqual(house["currency"], "USD")
        self.assertTrue(house["id"])

        updated = update_manual_asset(house["id"], "Primary home", "450000", "USD")
        self.assertEqual(updated["name"], "Primary home")
        self.assertEqual(updated["value"], "450000")
        self.assertEqual(get_manual_assets(), [updated])

        self.assertTrue(delete_manual_asset(house["id"]))
        self.assertFalse(delete_manual_asset(house["id"]))
        self.assertEqual(get_manual_assets(), [])

    def test_asset_name_and_value_are_encrypted_at_rest(self):
        asset = create_manual_asset("Private property", "123456.78", "USD")
        db = SessionLocal()
        try:
            stored = db.query(ManualAsset).filter(ManualAsset.id == asset["id"]).one()
            self.assertNotEqual(stored._name, "Private property")
            self.assertNotEqual(stored._value, "123456.78")
        finally:
            db.close()

    def test_invalid_manual_asset_values_are_rejected(self):
        for name, value, currency in (
            ("", "1", "USD"),
            ("House", "not-a-number", "USD"),
            ("House", "-1", "USD"),
            ("House", "NaN", "USD"),
            ("House", "1e100", "USD"),
            ("House", "1.1234567890123456789", "USD"),
            ("House", "1", "US dollars"),
        ):
            with self.subTest(name=name, value=value, currency=currency):
                with self.assertRaises(ValueError):
                    create_manual_asset(name, value, currency)
        self.assertEqual(get_manual_assets(), [])

    def test_simplefin_sync_does_not_modify_manual_assets(self):
        house = create_manual_asset("Home", "425000.00", "USD")
        save_simplefin_data({"accounts": [{
            "id": house["id"],
            "org": {"name": "SimpleFIN"},
            "name": "Checking",
            "balance": "100.00",
            "currency": "USD",
            "transactions": [],
        }]})

        self.assertEqual(get_manual_assets(), [house])
        db = SessionLocal()
        try:
            self.assertEqual(db.query(ManualAsset).count(), 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
