#!/usr/bin/env python3
"""Offline protocol tests for the deployed Finance SQL client."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

CLIENT_PATH = Path(__file__).with_name("finance-sql.py")
SPEC = importlib.util.spec_from_file_location("finance_sql_client", CLIENT_PATH)
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class FakeWebSocket:
    def __init__(self):
        self.messages = [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True},
        ]
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def send(self, raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message.get("type") == "call_service":
            payload = message["service_data"]["input"]
            self.messages.extend([
                {"id": 2, "type": "result", "success": True, "result": {}},
                {
                    "id": 1,
                    "type": "event",
                    "event": {
                        "event_type": client.EVENT_PREFIX + payload["request_id"],
                        "data": {"ok": True, "request_id": payload["request_id"], "rows": [["fixture"]]},
                    },
                },
            ])

    def recv(self):
        return json.dumps(self.messages.pop(0))

    def close(self):
        pass


class FinanceSqlClientTests(unittest.TestCase):
    def test_websocket_url_uses_home_assistant_base(self):
        self.assertEqual(client.websocket_url("http://ha.local:8123"), "ws://ha.local:8123/api/websocket")
        self.assertEqual(client.websocket_url("https://ha.local:8123/"), "wss://ha.local:8123/api/websocket")

    def test_client_subscribes_then_calls_addon_stdin_and_returns_correlated_event(self):
        socket = FakeWebSocket()
        with patch.dict(os.environ, {"HA_URL": "http://ha.local:8123", "HA_TOKEN": "unit-test-token"}), \
             patch.object(client.websocket, "create_connection", return_value=socket):
            result = client.run_statement("SELECT 1", apply=False, timeout=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [["fixture"]])
        auth = next(item for item in socket.sent if item.get("type") == "auth")
        self.assertEqual(auth["access_token"], "unit-test-token")
        service = next(item for item in socket.sent if item.get("type") == "call_service")
        self.assertEqual(service["domain"], "hassio")
        self.assertEqual(service["service"], "app_stdin")
        self.assertEqual(service["service_data"]["app"], "custom_finance")
        self.assertFalse(service["service_data"]["input"]["apply"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
