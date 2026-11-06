#!/usr/bin/env python3
"""Opt-in Supervisor store refresh and Finance Dashboard update worker."""
from __future__ import annotations

import json
import logging
import os
import re
import select
import sys
import time
import urllib.error
import urllib.request
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finance_dashboard_updater")

SUPERVISOR_BASE = "http://supervisor"
CORE_BASE = "http://supervisor/core/api"
FINANCE_APP = "10d6f312_custom_finance"
MAX_STDIN_BYTES = 32768
STORE_REFRESH_TIMEOUT_SECONDS = 120
UPDATE_TIMEOUT_SECONDS = 900


class UpdaterError(RuntimeError):
    pass


class SupervisorClient:
    def __init__(self):
        self.token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not self.token:
            raise UpdaterError("Supervisor token is unavailable; check add-on API permissions.")

    def request(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            SUPERVISOR_BASE + path,
            data=body,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise UpdaterError(f"Supervisor request {method} {path} failed with HTTP {exc.code}.") from None
        except Exception as exc:
            raise UpdaterError(f"Supervisor request {method} {path} failed ({type(exc).__name__}).") from None
        try:
            envelope = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise UpdaterError("Supervisor returned invalid JSON.") from None
        if isinstance(envelope, dict) and envelope.get("result") == "error":
            error_key = envelope.get("error_key")
            safe_key = error_key if isinstance(error_key, str) and re.fullmatch(r"[a-z0-9_]{1,80}", error_key) else "request_rejected"
            raise UpdaterError(f"Supervisor rejected {method} {path} ({safe_key}).")
        if isinstance(envelope, dict) and envelope.get("result") == "ok":
            return envelope.get("data") or {}
        return envelope


def version_key(value: str):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value or "")
    if not match:
        raise UpdaterError(f"Invalid Finance Dashboard version: {value!r}")
    return tuple(map(int, match.groups()))


def enable_dev_mode(payload: dict, supervisor: SupervisorClient | None = None):
    request_id = payload.get("request_id")
    try:
        request_id = uuid.UUID(hex=request_id).hex
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "command": "enable_dev_mode", "error": "request_id must be a UUID hex string."}
    if payload.get("confirmation") != "ENABLE FINANCE UPDATER DEV MODE":
        return {"ok": False, "command": "enable_dev_mode", "request_id": request_id, "error": "Explicit dev_mode confirmation is required."}
    try:
        supervisor = supervisor or SupervisorClient()
        options = supervisor.request("GET", "/addons/self/options/config")
        if not isinstance(options, dict):
            raise UpdaterError("Could not read updater options.")
        updated_options = {**options, "dev_mode": True}
        supervisor.request("POST", "/addons/self/options", {"options": updated_options})
        verified = supervisor.request("GET", "/addons/self/options/config")
        if not isinstance(verified, dict) or verified.get("dev_mode") is not True:
            raise UpdaterError("Supervisor did not persist dev_mode.")
        return {"ok": True, "command": "enable_dev_mode", "request_id": request_id, "dev_mode": True}
    except Exception as exc:
        return {
            "ok": False,
            "command": "enable_dev_mode",
            "request_id": request_id,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def refresh_and_update(
    payload: dict,
    supervisor: SupervisorClient | None = None,
    store_refresh_timeout: int = STORE_REFRESH_TIMEOUT_SECONDS,
    update_timeout: int = UPDATE_TIMEOUT_SECONDS,
    poll_interval: float = 3,
):
    request_id = payload.get("request_id")
    try:
        request_id = uuid.UUID(hex=request_id).hex
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "command": "refresh_and_update", "error": "request_id must be a UUID hex string."}

    target_version = payload.get("target_version")
    try:
        target_key = version_key(target_version)
    except UpdaterError as exc:
        return {"ok": False, "command": "refresh_and_update", "request_id": request_id, "error": str(exc)}

    try:
        supervisor = supervisor or SupervisorClient()
        options = supervisor.request("GET", "/addons/self/options/config")
        if not isinstance(options, dict) or options.get("dev_mode") is not True:
            if payload.get("enable_dev_mode") is True:
                enabled = enable_dev_mode({
                    "command": "enable_dev_mode",
                    "request_id": request_id,
                    "confirmation": payload.get("confirmation"),
                }, supervisor)
                if not enabled.get("ok"):
                    raise UpdaterError(enabled.get("error", "Could not enable updater dev_mode."))
            else:
                raise UpdaterError("Updater is disabled. Run the deploy script once with --enable-dev-mode.")

        supervisor.request("POST", "/store/reload", {})
        deadline = time.monotonic() + store_refresh_timeout
        latest_version = ""
        while time.monotonic() < deadline:
            app = supervisor.request("GET", f"/store/addons/{FINANCE_APP}")
            latest_version = str(app.get("version_latest", ""))
            latest_key = version_key(latest_version)
            if latest_key > target_key:
                raise UpdaterError(
                    f"Store reports Finance Dashboard {latest_version}, newer than requested {target_version}; refusing downgrade."
                )
            if latest_key == target_key:
                break
            time.sleep(poll_interval)
        else:
            raise UpdaterError(
                f"Store refresh did not advertise Finance Dashboard {target_version} "
                f"(last observed {latest_version or 'unknown'}). No update was started."
            )

        update = supervisor.request("POST", f"/store/addons/{FINANCE_APP}/update", {
            "backup": False,
            "background": True,
        })
        job_id = update.get("job_id") if isinstance(update, dict) else None
        if job_id:
            deadline = time.monotonic() + update_timeout
            while time.monotonic() < deadline:
                job = supervisor.request("GET", f"/jobs/{job_id}")
                if job.get("done"):
                    errors = job.get("errors") or []
                    if errors:
                        raise UpdaterError("Supervisor update job completed with errors.")
                    break
                time.sleep(poll_interval)
            else:
                raise UpdaterError("Update job timed out; inspect Supervisor before retrying.")

        deadline = time.monotonic() + update_timeout
        installed_version = ""
        while time.monotonic() < deadline:
            info = supervisor.request("GET", f"/addons/{FINANCE_APP}/info")
            installed_version = str(info.get("version", ""))
            if installed_version and version_key(installed_version) >= target_key and info.get("state") == "started":
                return {
                    "ok": True,
                    "command": "refresh_and_update",
                    "request_id": request_id,
                    "refreshed_version": latest_version,
                    "installed_version": installed_version,
                    "backup": False,
                }
            time.sleep(poll_interval)
        raise UpdaterError(
            f"Timed out verifying Finance Dashboard {target_version} (last installed version {installed_version or 'unknown'})."
        )
    except Exception as exc:
        return {
            "ok": False,
            "command": "refresh_and_update",
            "request_id": request_id,
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }


def publish_result(result: dict):
    request_id = result.get("request_id")
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token or not request_id:
        return False
    event_type = f"finance_updater_result_{request_id}"
    request = urllib.request.Request(
        f"{CORE_BASE}/events/{event_type}",
        data=json.dumps(result, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        logger.error("Could not publish updater result (%s).", type(exc).__name__)
        return False


def process_message(message: bytes):
    if len(message) > MAX_STDIN_BYTES:
        result = {"ok": False, "error": "Updater command exceeds the size limit."}
    else:
        try:
            payload = json.loads(message.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Updater command must be a JSON object.")
            command = payload.get("command")
            if command == "enable_dev_mode":
                result = enable_dev_mode(payload)
            elif command == "refresh_and_update":
                result = refresh_and_update(payload)
            else:
                raise ValueError("Unknown updater command.")
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    if result.get("request_id"):
        delivered = publish_result(result)
        print("FINANCE_UPDATER_RESULT " + json.dumps({
            "ok": bool(result.get("ok")) and delivered,
            "request_id": result.get("request_id"),
            "result_delivered": delivered,
        }, separators=(",", ":")), flush=True)
    else:
        print("FINANCE_UPDATER_RESULT " + json.dumps({"ok": False, "error": result.get("error", "Invalid request.")}), flush=True)


def main():
    try:
        stdin_fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        logger.error("Updater stdin is unavailable.")
        return
    logger.info("Finance updater ready; dev_mode is checked for each command.")
    buffer = bytearray()
    while True:
        try:
            readable, _, _ = select.select([stdin_fd], [], [], 0.5)
            if not readable:
                if buffer:
                    process_message(bytes(buffer))
                    buffer.clear()
                continue
            chunk = os.read(stdin_fd, 4096)
            if not chunk:
                if buffer:
                    process_message(bytes(buffer))
                return
            buffer.extend(chunk)
            if b"\n" in buffer:
                message, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                process_message(bytes(message))
            elif len(buffer) > MAX_STDIN_BYTES:
                process_message(bytes(buffer))
                buffer.clear()
        except Exception as exc:
            logger.error("Updater stdin worker stopped (%s).", type(exc).__name__)
            return


if __name__ == "__main__":
    main()
