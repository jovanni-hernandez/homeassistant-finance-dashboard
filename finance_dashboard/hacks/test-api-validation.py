#!/usr/bin/env python3
"""Unit checks for bounded API rule inputs."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
TEST_ROOT = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
TEST_ROOT.mkdir(parents=True, exist_ok=True)
TEST_DATA = tempfile.TemporaryDirectory(prefix="api-validation-", dir=TEST_ROOT)
os.environ["FINANCE_DB_PATH"] = str(Path(TEST_DATA.name) / "test.sqlite")
os.environ["FINANCE_KEY_PATH"] = str(Path(TEST_DATA.name) / "test.key")
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from main import validate_category_rules_json, validate_recurring_rules_json  # noqa: E402
import security  # noqa: E402


class ApiValidationTests(unittest.TestCase):
    def test_category_rules_accept_valid_json(self):
        validate_category_rules_json('[{"category":"Subscriptions","patterns":["spotify"]}]')

    def test_category_rules_reject_malformed_or_unbounded_values(self):
        for raw in ("not-json", "{}", '[{"category":"","patterns":[]} ]', '[{"category":"Food","patterns":"x"}]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_category_rules_json(raw)

    def test_recurring_rules_accept_valid_json(self):
        validate_recurring_rules_json('{"ignored_keys":["spotify"],"overrides":{"netflix":{"kind":"subscription","hidden":false}}}')

    def test_recurring_rules_reject_bad_kinds(self):
        with self.assertRaises(ValueError):
            validate_recurring_rules_json('{"overrides":{"merchant":{"kind":"anything"}}}')

    def test_encryption_failure_never_returns_plaintext(self):
        class BrokenCipher:
            def encrypt(self, _value):
                raise ValueError("simulated failure")

        original = security.cipher
        try:
            security.cipher = BrokenCipher()
            with self.assertRaises(RuntimeError):
                security.encrypt_value("sensitive-value")
        finally:
            security.cipher = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
