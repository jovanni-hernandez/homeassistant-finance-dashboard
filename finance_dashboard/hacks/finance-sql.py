#!/usr/bin/env python3
"""Run one SQL statement against the deployed Finance add-on database via HA stdin."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import websocket

ADDON_SLUG = "custom_finance"
EVENT_PREFIX = "finance_sql_result_"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql", nargs="?", help="One SQL statement. If omitted, read it from stdin.")
    parser.add_argument("--write", action="store_true", help="Allow writes; must be combined with --apply.")
    parser.add_argument("--apply", action="store_true", help="Commit the SQL statement; otherwise database writes are blocked.")
    parser.add_argument("--timeout", type=int, default=30, help="Seconds to wait for the add-on result (default: 30).")
    return parser.parse_args()


def websocket_url(ha_url: str) -> str:
    parsed = urlsplit(ha_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HA_URL must be an http:// or https:// Home Assistant base URL.")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = parsed.path.rstrip("/") + "/api/websocket"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def receive_json(connection, deadline: float):
    while time.monotonic() < deadline:
        connection.settimeout(max(0.1, deadline - time.monotonic()))
        message = connection.recv()
        if message:
            return json.loads(message)
    raise TimeoutError("Timed out waiting for Home Assistant.")


def run_statement(statement: str, apply: bool, timeout: int) -> dict:
    ha_url = os.environ.get("HA_URL", "")
    token = os.environ.get("HA_TOKEN", "")
    if not ha_url or not token:
        raise RuntimeError("HA_URL and HA_TOKEN are required; load them from finance_dashboard/.env.")

    request_id = uuid.uuid4().hex
    event_type = EVENT_PREFIX + request_id
    payload = {"command": "sql", "request_id": request_id, "sql": statement, "apply": apply}
    finance_session = os.environ.get("FINANCE_SESSION_TOKEN")
    if finance_session:
        payload["finance_session"] = finance_session

    connection = websocket.create_connection(websocket_url(ha_url), timeout=timeout)
    deadline = time.monotonic() + timeout
    try:
        if receive_json(connection, deadline).get("type") != "auth_required":
            raise RuntimeError("Home Assistant did not start the expected WebSocket authentication flow.")
        connection.send(json.dumps({"type": "auth", "access_token": token}))
        if receive_json(connection, deadline).get("type") != "auth_ok":
            raise RuntimeError("Home Assistant rejected the WebSocket authentication.")

        connection.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": event_type}))
        subscription = receive_json(connection, deadline)
        if subscription.get("id") != 1 or not subscription.get("success"):
            raise RuntimeError("Could not subscribe to the private SQL result event.")

        connection.send(json.dumps({
            "id": 2,
            "type": "call_service",
            "domain": "hassio",
            "service": "app_stdin",
            "service_data": {"app": ADDON_SLUG, "input": payload},
        }))
        while time.monotonic() < deadline:
            message = receive_json(connection, deadline)
            if message.get("id") == 2 and not message.get("success"):
                error = message.get("error", {}).get("message", "service call failed")
                raise RuntimeError(f"Home Assistant rejected the add-on stdin call: {error}")
            event_data = message.get("event", {}).get("data", {})
            if event_data.get("request_id") == request_id:
                return event_data
        raise TimeoutError("SQL result timed out. The statement may have run; inspect before retrying.")
    finally:
        connection.close()


def main() -> int:
    args = parse_args()
    if args.timeout < 1 or args.timeout > 300:
        print("--timeout must be between 1 and 300 seconds.", file=sys.stderr)
        return 2
    if args.write != args.apply:
        print("Writes require both --write and --apply; otherwise use read-only mode.", file=sys.stderr)
        return 2
    statement = args.sql if args.sql is not None else sys.stdin.read()
    if not statement.strip():
        print("Provide one SQL statement as an argument or on stdin.", file=sys.stderr)
        return 2
    try:
        result = run_statement(statement, apply=args.apply, timeout=args.timeout)
    except Exception as exc:
        print(f"Finance SQL failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
