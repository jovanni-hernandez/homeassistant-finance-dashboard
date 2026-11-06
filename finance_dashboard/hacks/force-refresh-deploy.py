#!/usr/bin/env python3
"""Use the optional manager-role updater add-on to refresh the store and deploy this release."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import websocket

HELPER_APP = "10d6f312_finance_updater"
EVENT_PREFIX = "finance_updater_result_"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=1200, help="Seconds to wait for refresh and update (default: 1200).")
    parser.add_argument("--enable-dev-mode", action="store_true", help="Persist dev_mode=true on the optional updater add-on for this installation.")
    return parser.parse_args()


def version_from_config() -> str:
    import re

    path = Path(__file__).resolve().parents[1] / "config.yaml"
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", path.read_text(), re.MULTILINE)
    if not match:
        raise RuntimeError("Could not read Finance Dashboard version from config.yaml.")
    return match.group(1)


def ws_url(ha_url: str) -> str:
    parsed = urlsplit(ha_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HA_URL must be an http:// or https:// Home Assistant base URL.")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, parsed.path.rstrip("/") + "/api/websocket", "", ""))


def receive_json(connection, deadline: float):
    while time.monotonic() < deadline:
        connection.settimeout(max(0.1, deadline - time.monotonic()))
        message = connection.recv()
        if message:
            return json.loads(message)
    raise TimeoutError("Timed out waiting for Home Assistant.")


def _call_service(connection, call_id: int, service: str, service_data: dict, deadline: float, event_type: str | None = None):
    connection.send(json.dumps({
        "id": call_id,
        "type": "call_service",
        "domain": "hassio",
        "service": service,
        "service_data": service_data,
    }))
    event_result = None
    while time.monotonic() < deadline:
        message = receive_json(connection, deadline)
        event = message.get("event", {})
        if event_type and event.get("event_type") == event_type:
            event_result = event.get("data", {})
        if message.get("id") != call_id:
            continue
        if not message.get("success"):
            error = message.get("error", {}).get("message", "service call failed")
            raise RuntimeError(f"Home Assistant rejected hassio.{service}: {error}")
        return event_result
    raise TimeoutError(f"Timed out waiting for hassio.{service} acknowledgment.")


def run(target_version: str, timeout: int, enable_dev_mode: bool = False) -> dict:
    ha_url = os.environ.get("HA_URL", "")
    token = os.environ.get("HA_TOKEN", "")
    if not ha_url or not token:
        raise RuntimeError("HA_URL and HA_TOKEN are required; load them from the ignored .env file.")
    request_id = uuid.uuid4().hex
    event_type = EVENT_PREFIX + request_id
    event_result = None
    connection = websocket.create_connection(ws_url(ha_url), timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        if receive_json(connection, deadline).get("type") != "auth_required":
            raise RuntimeError("Home Assistant did not start the expected WebSocket authentication flow.")
        connection.send(json.dumps({"type": "auth", "access_token": token}))
        if receive_json(connection, deadline).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected WebSocket authentication.")
        connection.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": event_type}))
        subscribed = receive_json(connection, deadline)
        if subscribed.get("id") != 1 or not subscribed.get("success"):
            raise RuntimeError("Could not subscribe to the private updater result event.")

        _call_service(connection, 2, "app_start", {"app": HELPER_APP}, deadline)
        event_result = _call_service(connection, 3, "app_stdin", {
            "app": HELPER_APP,
            "input": {
                "command": "refresh_and_update",
                "request_id": request_id,
                "target_version": target_version,
                "enable_dev_mode": enable_dev_mode,
                "confirmation": "ENABLE FINANCE UPDATER DEV MODE" if enable_dev_mode else "",
            },
        }, deadline, event_type=event_type)

        while event_result is None and time.monotonic() < deadline:
            message = receive_json(connection, deadline)
            event = message.get("event", {})
            data = event.get("data", {})
            if event.get("event_type") == event_type and data.get("request_id") == request_id:
                event_result = data
        if event_result is None:
            raise TimeoutError("Updater result timed out; inspect Supervisor jobs before retrying.")
        return event_result
    finally:
        try:
            _call_service(connection, 4, "app_stop", {"app": HELPER_APP}, time.monotonic() + 20)
        except Exception:
            pass
        connection.close()


def main() -> int:
    args = parse_args()
    if args.timeout < 30 or args.timeout > 1800:
        print("--timeout must be between 30 and 1800 seconds.", file=sys.stderr)
        return 2
    try:
        result = run(version_from_config(), args.timeout, enable_dev_mode=args.enable_dev_mode)
    except Exception as exc:
        print(f"Finance deploy failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
