#!/usr/bin/env python3
"""Offline WebSocket protocol tests for the optional updater client."""
from __future__ import annotations

import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).with_name("force-refresh-deploy.py")
SPEC = importlib.util.spec_from_file_location("force_refresh_deploy", SCRIPT_PATH)
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
        if message.get("type") != "call_service":
            return
        call_id = message["id"]
        service = message["service"]
        if service == "app_stdin":
            payload = message["service_data"]["input"]
            request_id = payload["request_id"]
            event_type = client.EVENT_PREFIX + request_id
            self.messages.extend([
                {"id": 1, "type": "event", "event": {
                    "event_type": event_type,
                    "data": {"ok": True, "request_id": request_id, "installed_version": payload["target_version"]},
                }},
                {"id": call_id, "type": "result", "success": True, "result": {}},
            ])
        else:
            self.messages.append({"id": call_id, "type": "result", "success": True, "result": {}})

    def recv(self):
        return json.dumps(self.messages.pop(0))

    def close(self):
        pass


class ForceRefreshDeployTests(unittest.TestCase):
    def test_client_starts_helper_sends_request_and_stops_helper(self):
        socket = FakeWebSocket()
        with patch.dict(os.environ, {"HA_URL": "http://ha.local:8123", "HA_TOKEN": "unit-test-token"}), \
             patch.object(client.websocket, "create_connection", return_value=socket):
            result = client.run("2.3.47", timeout=5)

        self.assertTrue(result["ok"])
        self.assertEqual(result["installed_version"], "2.3.47")
        calls = [message for message in socket.sent if message.get("type") == "call_service"]
        self.assertEqual([call["service"] for call in calls], ["app_start", "app_stdin", "app_stop"])
        self.assertEqual(calls[0]["service_data"]["app"], client.HELPER_APP)
        request = calls[1]["service_data"]["input"]
        self.assertEqual(request["command"], "refresh_and_update")
        self.assertEqual(request["target_version"], "2.3.47")
        self.assertEqual(calls[2]["service_data"]["app"], client.HELPER_APP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
