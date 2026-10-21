#!/usr/bin/env python3
"""Unit-test the Supervisor REST-proxy release flow without changing Home Assistant."""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_release  # noqa: E402


class SupervisorStub(BaseHTTPRequestHandler):
    slug = ""
    version = ""
    seen: list[tuple[str, str, dict | None, str | None]] = []
    update_requested = False

    def log_message(self, *_args):
        return

    def reply(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        type(self).seen.append(("POST", self.path, payload, self.headers.get("Authorization")))
        if self.path == "/api/hassio/store/reload":
            return self.reply({"result": "ok", "data": {}})
        if self.path == f"/api/hassio/store/addons/{self.slug}/update":
            type(self).update_requested = True
            return self.reply({"result": "ok", "data": {"job_id": "test-job"}})
        return self.reply({"result": "error", "message": "unexpected POST"})

    def do_GET(self):
        type(self).seen.append(("GET", self.path, None, self.headers.get("Authorization")))
        if self.path == f"/api/hassio/store/addons/{self.slug}":
            return self.reply({"result": "ok", "data": {
                "version": "2.2.34",
                "version_latest": self.version,
                "update_available": True,
            }})
        if self.path == "/api/hassio/jobs/test-job":
            return self.reply({"result": "ok", "data": {"done": True, "errors": []}})
        if self.path == f"/api/hassio/addons/{self.slug}/info":
            version = self.version if type(self).update_requested else "2.2.34"
            return self.reply({"result": "ok", "data": {"version": version, "state": "started"}})
        return self.reply({"result": "error", "message": "unexpected GET"})


class ReleaseFlowTest(unittest.TestCase):
    def test_refresh_update_job_and_installed_version_verification(self):
        slug, version = ha_release.read_config()
        SupervisorStub.slug = slug
        SupervisorStub.version = version
        SupervisorStub.seen = []
        SupervisorStub.update_requested = False
        server = ThreadingHTTPServer(("127.0.0.1", 0), SupervisorStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {
                "HA_URL": f"http://127.0.0.1:{server.server_port}",
                "HA_TOKEN": "unit-test-token",
            }):
                client = ha_release.HAProxy()
                result = ha_release.deploy(client, slug, version, attempts=2, delay=0)

            self.assertEqual(result["installed_version"], version)
            self.assertTrue(result["updated"])
            self.assertTrue(SupervisorStub.update_requested)

            calls = {(method, path): (payload, auth) for method, path, payload, auth in SupervisorStub.seen}
            self.assertIn(("POST", "/api/hassio/store/reload"), calls)
            update_payload, update_auth = calls[("POST", f"/api/hassio/store/addons/{slug}/update")]
            self.assertIs(update_payload["backup"], False)
            self.assertIs(update_payload["background"], True)
            self.assertEqual(update_auth, "Bearer unit-test-token")
            self.assertIn(("GET", "/api/hassio/jobs/test-job"), calls)
            self.assertIn(("GET", f"/api/hassio/addons/{slug}/info"), calls)
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
