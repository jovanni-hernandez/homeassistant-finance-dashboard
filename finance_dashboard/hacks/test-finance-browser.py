#!/usr/bin/env python3
"""Offline guardrail tests for the Finance Selenium controller."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("finance-browser.py")
SPEC = importlib.util.spec_from_file_location("finance_browser", SCRIPT_PATH)
browser = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(browser)


class FinanceBrowserTests(unittest.TestCase):
    def test_write_capable_actions_need_apply(self):
        for action in ("click", "fill", "select", "keypress"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                browser.validate_apply([{"type": action}], apply=False)
            browser.validate_apply([{"type": action}], apply=True)

    def test_read_only_actions_do_not_need_apply(self):
        browser.validate_apply([
            {"type": "wait"},
            {"type": "count"},
            {"type": "screenshot"},
        ], apply=False)

    def test_screenshot_name_cannot_escape_artifact_directory(self):
        self.assertEqual(browser.safe_screenshot_name("step-1.png"), "step-1.png")
        with self.assertRaises(ValueError):
            browser.safe_screenshot_name("../secret.png")


if __name__ == "__main__":
    unittest.main(verbosity=2)
