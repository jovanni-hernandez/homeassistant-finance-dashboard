#!/usr/bin/env python3
"""Offline policy tests for the opt-in Supervisor updater add-on."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "finance_dashboard_updater" / "updater.py"
SPEC = importlib.util.spec_from_file_location("finance_dashboard_updater", SCRIPT_PATH)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class FakeSupervisor:
    def __init__(self, dev_mode=True, latest="2.3.47", installed="2.3.47"):
        self.dev_mode = dev_mode
        self.options = {"dev_mode": dev_mode, "unrelated_option": "preserved"}
        self.latest = latest
        self.installed = installed
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/addons/self/options/config":
            return dict(self.options)
        if method == "POST" and path == "/addons/self/options":
            self.options = dict(payload["options"])
            self.dev_mode = self.options.get("dev_mode") is True
            return {}
        if method == "POST" and path == "/store/reload":
            return {}
        if method == "GET" and path == "/store/addons/10d6f312_custom_finance":
            return {"version_latest": self.latest}
        if method == "POST" and path == "/store/addons/10d6f312_custom_finance/update":
            self.installed = self.latest
            return {"job_id": "test-job"}
        if method == "GET" and path == "/jobs/test-job":
            return {"done": True, "errors": []}
        if method == "GET" and path == "/addons/10d6f312_custom_finance/info":
            return {"version": self.installed, "state": "started"}
        raise AssertionError(f"Unexpected Supervisor request: {method} {path}")


class FinanceUpdaterTests(unittest.TestCase):
    def payload(self):
        return {"command": "refresh_and_update", "request_id": "a" * 32, "target_version": "2.3.47"}

    def test_dev_mode_off_blocks_before_store_refresh(self):
        supervisor = FakeSupervisor(dev_mode=False)
        result = updater.refresh_and_update(self.payload(), supervisor)
        self.assertFalse(result["ok"])
        self.assertEqual([call[1] for call in supervisor.calls], ["/addons/self/options/config"])

    def test_explicit_enable_dev_mode_preserves_other_options(self):
        supervisor = FakeSupervisor(dev_mode=False)
        result = updater.enable_dev_mode({
            "request_id": "b" * 32,
            "confirmation": "ENABLE FINANCE UPDATER DEV MODE",
        }, supervisor)
        self.assertTrue(result["ok"])
        self.assertEqual(supervisor.options, {"dev_mode": True, "unrelated_option": "preserved"})

    def test_refresh_and_update_uses_no_backup_and_verifies_installed_version(self):
        supervisor = FakeSupervisor(installed="2.3.46")
        result = updater.refresh_and_update(self.payload(), supervisor)
        self.assertTrue(result["ok"])
        self.assertEqual(result["installed_version"], "2.3.47")
        self.assertIn(("POST", "/store/reload", {}), supervisor.calls)
        update_call = next(call for call in supervisor.calls if call[1].endswith("/update"))
        self.assertIs(update_call[2]["backup"], False)

    def test_current_install_is_a_successful_noop_after_store_refresh(self):
        supervisor = FakeSupervisor(installed="2.3.47")
        result = updater.refresh_and_update(self.payload(), supervisor)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "already_current")
        self.assertFalse(any(call[1].endswith("/update") for call in supervisor.calls))

    def test_stale_store_version_fails_without_starting_update(self):
        supervisor = FakeSupervisor(latest="2.3.46", installed="2.3.45")
        result = updater.refresh_and_update(
            self.payload(), supervisor, store_refresh_timeout=0.01, poll_interval=0.001,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(any(call[1].endswith("/update") for call in supervisor.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
