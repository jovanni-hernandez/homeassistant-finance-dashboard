#!/usr/bin/env python3
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
import time
from pathlib import Path

from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def parse_args():
    parser = argparse.ArgumentParser(description="Validate the finance dashboard UI with Selenium.")
    parser.add_argument("--file", default="finance_dashboard/frontend/index.html")
    parser.add_argument("--screenshot", default="/data/v2/cache/opencode/finance-dashboard-selenium.png")
    parser.add_argument("--timeout", type=int, default=20)
    return parser.parse_args()


def main():
    args = parse_args()
    abs_input = Path(args.file).resolve()
    screenshot = Path(args.screenshot)
    screenshot.parent.mkdir(parents=True, exist_ok=True)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A003
            return

        def _send(self, status, body, content_type="text/html; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            parsed_path = self.path.split("?", 1)[0]
            if parsed_path in {"/", "/frontend/index.html"}:
                return self._send(200, abs_input.read_text())
            if parsed_path == "/api/status":
                return self._send(200, json.dumps({
                    "connected": True,
                    "account_count": 1,
                    "transaction_count": 3,
                    "last_sync_status": "success",
                    "last_sync_message": "Stubbed sync",
                    "last_sync_at": "2026-10-21T00:00:00Z",
                    "has_token": True,
                }), "application/json; charset=utf-8")
            if parsed_path == "/api/pin/status":
                return self._send(200, json.dumps({
                    "enabled": False,
                    "configured": True,
                    "pin_required": False,
                    "session_active": True,
                    "locked": False,
                    "idle_timeout_minutes": 10,
                    "session_timeout_minutes": 480,
                }), "application/json; charset=utf-8")
            if parsed_path == "/api/accounts":
                return self._send(200, json.dumps({"accounts": [{"id": "acct1", "org": "Stub Bank", "name": "Checking", "balance": "4200.00", "currency": "USD", "updated_at": "2026-10-21T00:00:00Z"}]}), "application/json; charset=utf-8")
            if parsed_path == "/api/transactions":
                return self._send(200, json.dumps({"transactions": [
                    {"id": "tx1", "account_id": "acct1", "account_name": "Checking", "org": "Checking", "date": 1761000000, "amount": "-12.99", "payee": "Spotify", "description": "Spotify Premium", "category": "Subscriptions", "pending": 0},
                    {"id": "tx2", "account_id": "acct1", "account_name": "Checking", "org": "Checking", "date": 1760400000, "amount": "-12.99", "payee": "Spotify", "description": "Spotify Premium", "category": "Subscriptions", "pending": 0},
                    {"id": "tx3", "account_id": "acct1", "account_name": "Checking", "org": "Checking", "date": 1759800000, "amount": "2500.00", "payee": "Payroll", "description": "Payroll deposit", "category": "Income", "pending": 0},
                ]}), "application/json; charset=utf-8")
            if parsed_path == "/api/recurring-payments":
                return self._send(200, json.dumps({
                    "generated_at": "2026-10-21T00:00:00Z",
                    "transaction_count": 3,
                    "recurring_count": 1,
                    "summary": {"subscription_count": 1, "recurring_bill_count": 0, "recurring_income_count": 0, "other_count": 0},
                    "items": [{
                        "merchant_key": "spotify",
                        "label": "Spotify",
                        "kind": "subscription",
                        "cadence_days": 30.0,
                        "cadence_label": "monthly",
                        "count": 2,
                        "span_days": 70.0,
                        "first_seen": "2026-08-10T00:00:00Z",
                        "last_seen": "2026-10-20T00:00:00Z",
                        "next_expected": "2026-11-19T00:00:00Z",
                        "average_amount": 12.99,
                        "min_amount": 12.99,
                        "max_amount": 12.99,
                        "amount_stability": 0.0,
                        "confidence": 92,
                        "examples": [
                            {"date": "2026-08-10T00:00:00Z", "amount": -12.99, "payee": "Spotify", "description": "Spotify Premium", "account_name": "Checking"},
                            {"date": "2026-10-20T00:00:00Z", "amount": -12.99, "payee": "Spotify", "description": "Spotify Premium", "account_name": "Checking"},
                        ],
                        "account_names": ["Checking"],
                        "is_overridden": False,
                    }],
                }), "application/json; charset=utf-8")
            if parsed_path == "/api/category-rules":
                return self._send(200, json.dumps({"rules_json": "[]"}), "application/json; charset=utf-8")
            if parsed_path == "/api/recurring-rules":
                return self._send(200, json.dumps({"recurring_rules_json": "{\"ignored_keys\":[],\"overrides\":{}}"}), "application/json; charset=utf-8")
            if parsed_path == "/api/pin/settings":
                return self._send(200, json.dumps({"enabled": False, "idle_timeout_minutes": 10, "session_timeout_minutes": 480}), "application/json; charset=utf-8")
            if parsed_path == "/api/ai/settings":
                return self._send(200, json.dumps({"enabled": False, "provider": "openai", "model": "gpt-4o-mini"}), "application/json; charset=utf-8")
            if parsed_path == "/api/category-defaults":
                return self._send(200, json.dumps({"status": "success", "transaction_count": 3, "proposals": [], "rules_json": "[]"}), "application/json; charset=utf-8")
            if parsed_path == "/api/category-defaults/ai":
                return self._send(200, json.dumps({"status": "success", "transaction_count": 3, "proposals": [], "rules_json": "[]"}), "application/json; charset=utf-8")
            if parsed_path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            return self._send(404, "Not found", "text/plain; charset=utf-8")

        def do_POST(self):
            parsed_path = self.path.split("?", 1)[0]
            if parsed_path.startswith("/api/"):
                return self._send(200, json.dumps({"status": "success", "message": "stub"}), "application/json; charset=utf-8")
            return self._send(404, "Not found", "text/plain; charset=utf-8")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    url = f"http://127.0.0.1:{port}/"

    options = Options()
    options.binary_location = "/usr/bin/chromium"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            try:
                driver.set_page_load_timeout(args.timeout)
                driver.get(url)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise TimeoutError(f"Timed out loading {url}")

        wait = WebDriverWait(driver, args.timeout)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        wait.until(lambda d: "Finance Dashboard" in d.title)

        dashboard_text = driver.find_element(By.TAG_NAME, "body").text
        logs = driver.get_log("browser")

        locked = "PIN required" in dashboard_text
        critical_dashboard_sections = ["Cash flow", "Recent transactions", "Accounts"]
        critical_recurring_sections = ["Recurring payments", "Hide low-confidence", "Subscriptions"]

        if not locked:
            for text in critical_dashboard_sections:
                wait.until(lambda d, needle=text: needle in d.find_element(By.TAG_NAME, "body").text)

            # Exercise the recurring screen directly.
            menu_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[aria-label="Open menu"]')))
            menu_button.click()
            recurring_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Recurring']]")))
            recurring_button.click()
            for text in critical_recurring_sections:
                wait.until(lambda d, needle=text: needle in d.find_element(By.TAG_NAME, "body").text)
            recurring_text = driver.find_element(By.TAG_NAME, "body").text
        else:
            recurring_text = dashboard_text

        boot_screen_visible = driver.execute_script(
            "const el = document.getElementById('boot-screen'); return !!el && getComputedStyle(el).display !== 'none' && getComputedStyle(el).visibility !== 'hidden' && el.offsetParent !== null;"
        )

        driver.save_screenshot(str(screenshot))
        image = Image.open(screenshot).convert("RGB")
        width, height = image.size
        gray_bytes = image.convert("L").tobytes()
        content_pixels = 0
        for value in gray_bytes:
            if value < 245:
                content_pixels += 1
        content_ratio = content_pixels / max(1, width * height)

        body_text_length = len(dashboard_text)
        severe_logs = []
        warnings = []
        for entry in logs:
            message = entry.get("message", "")
            level = entry.get("level", "")
            if level in {"SEVERE", "ERROR"}:
                severe_logs.append(message)
            if "[Vue warn]: Property" in message or "[Vue warn]: Unhandled error" in message:
                warnings.append(message)
        ok = (
            "Finance Dashboard" in driver.title
            and body_text_length > 50
            and (locked or all(text in dashboard_text for text in critical_dashboard_sections))
            and (locked or all(text in recurring_text for text in critical_recurring_sections))
            and content_ratio >= 0.003
            and not severe_logs
            and not warnings
            and not boot_screen_visible
        )

        result = {
            "ok": ok,
            "title": driver.title,
            "body_length": len(dashboard_text),
            "recurring_body_length": len(recurring_text),
            "screenshot": str(screenshot),
            "screenshot_content_ratio": round(content_ratio, 4),
            "browser_logs": len(logs),
            "locked": locked,
            "severe_logs": len(severe_logs),
            "vue_warnings": len(warnings),
            "boot_screen_visible": boot_screen_visible,
            "critical_dashboard_sections": critical_dashboard_sections,
            "critical_recurring_sections": critical_recurring_sections,
        }
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    finally:
        driver.quit()
        server.shutdown()
        server_thread.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    main()
