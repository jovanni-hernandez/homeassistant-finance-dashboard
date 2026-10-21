#!/usr/bin/env python3
"""Selenium smoke test against the real FastAPI app or an explicitly supplied live URL."""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="frontend/index.html", help="Frontend file; used to locate the backend in local mode")
    configured_url = os.environ.get("FINANCE_DASHBOARD_URL")
    if configured_url == "local":
        configured_url = None
    parser.add_argument("--url", default=configured_url, help="Optional deployed app URL to test directly")
    parser.add_argument("--screenshot", default="/data/v2/cache/opencode/finance-dashboard-real-selenium.png")
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_real_backend(frontend_file: Path):
    """Start the real FastAPI app with isolated DB/key and SimpleFIN mock fallback."""
    backend_dir = frontend_file.resolve().parent.parent / "backend"
    if not (backend_dir / "main.py").is_file():
        raise FileNotFoundError(f"Backend not found next to {frontend_file}: {backend_dir}")

    cache_root = Path("/data/v2/cache/opencode")
    cache_root.mkdir(parents=True, exist_ok=True)
    test_data_dir = tempfile.TemporaryDirectory(prefix="finance-dashboard-e2e-", dir=cache_root)
    os.environ["FINANCE_DB_PATH"] = str(Path(test_data_dir.name) / "test.sqlite")
    os.environ["FINANCE_KEY_PATH"] = str(Path(test_data_dir.name) / "test.key")
    sys.path.insert(0, str(backend_dir))
    os.chdir(backend_dir)  # StaticFiles uses ../frontend relative to the app working directory.

    import simplefin  # noqa: PLC0415
    import uvicorn  # noqa: PLC0415

    # Do not read the real add-on options/token in this isolated test. The real sync
    # function's built-in sample-data fallback seeds the test database instead.
    simplefin.get_simplefin_token = lambda: None

    import main  # noqa: PLC0415

    port = reserve_port()
    config = uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning", access_log=True)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="finance-api-test", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/"

    deadline = time.monotonic() + 45
    last_error = None
    while time.monotonic() < deadline:
        if not thread.is_alive():
            test_data_dir.cleanup()
            raise RuntimeError("Real FastAPI test server exited during startup")
        try:
            with urllib.request.urlopen(base_url + "api/pin/status", timeout=2) as response:
                if response.status == 200:
                    return base_url, server, thread, test_data_dir
        except Exception as exc:  # server may still be starting its DB and mock sync
            last_error = exc
            time.sleep(0.25)

    server.should_exit = True
    thread.join(timeout=5)
    test_data_dir.cleanup()
    raise TimeoutError(f"Real FastAPI app did not become ready: {last_error}")


def probe_api(base_url: str) -> dict[str, int]:
    endpoints = [
        "api/status",
        "api/pin/status",
        "api/accounts",
        "api/transactions",
        "api/category-rules",
        "api/recurring-rules",
        "api/recurring-payments",
        "api/pin/settings",
        "api/ai/settings",
    ]
    statuses = {}
    for endpoint in endpoints:
        request = urllib.request.Request(base_url + endpoint, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                statuses[endpoint] = response.status
                if response.status != 200:
                    raise RuntimeError(f"{endpoint} returned HTTP {response.status}")
                json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            statuses[endpoint] = exc.code
            raise RuntimeError(f"{endpoint} returned HTTP {exc.code}") from exc
    return statuses


def screenshot_content_ratio(path: Path) -> float:
    image = Image.open(path).convert("L")
    values = image.tobytes()
    return sum(value < 245 for value in values) / max(1, len(values))


def main():
    args = parse_args()
    screenshot = Path(args.screenshot)
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    frontend_file = Path(args.file).resolve()
    server = thread = test_data_dir = None

    try:
        if args.url:
            base_url = args.url.rstrip("/") + "/"
            test_mode = "deployed-url"
        else:
            base_url, server, thread, test_data_dir = start_real_backend(frontend_file)
            test_mode = "real-fastapi-isolated-db"

        api_statuses = probe_api(base_url)
        options = Options()
        options.binary_location = "/usr/bin/chromium"
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        driver = webdriver.Chrome(options=options)

        try:
            driver.set_window_size(1280, 1000)
            driver.set_page_load_timeout(args.timeout)
            driver.get(base_url)
            wait = WebDriverWait(driver, args.timeout)
            wait.until(lambda current: "Finance Dashboard" in current.title)
            wait.until(lambda current: current.execute_script("return !document.getElementById('boot-screen')"))
            wait.until(lambda current: "Loading financial data…" not in current.find_element(By.TAG_NAME, "body").text)

            dashboard_text = driver.find_element(By.TAG_NAME, "body").text
            render_snippets = driver.execute_script(
                "const app=document.querySelector('#app')?.__vue_app__;"
                "const text=(app?app._instance?.render?.toString():'')||'';"
                "return [...text.matchAll(/.{0,100}length.{0,100}/g)].map(match=>match[0]).slice(0,30);"
            )
            dashboard_sections = ["Cash flow", "Accounts", "Recent transactions"]
            for section in dashboard_sections:
                wait.until(lambda current, item=section: item in current.find_element(By.TAG_NAME, "body").text)
            driver.save_screenshot(str(screenshot))
            dashboard_ratio = screenshot_content_ratio(screenshot)

            menu = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[aria-label="Open menu"]')))
            menu.click()
            recurring_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Recurring']]")))
            recurring_button.click()
            recurring_sections = ["Recurring payments", "Subscriptions", "Hide low-confidence"]
            for section in recurring_sections:
                try:
                    wait.until(lambda current, item=section: item in current.find_element(By.TAG_NAME, "body").text)
                except Exception as exc:
                    current_text = driver.find_element(By.TAG_NAME, "body").text
                    print(json.dumps({
                        "failed_recurring_assertion": section,
                        "url": driver.current_url,
                        "body_length": len(current_text),
                        "browser_logs": driver.get_log("browser"),
                        "render_length_expressions": render_snippets,
                    }, indent=2))
                    raise RuntimeError(f"Recurring view did not render {section}") from exc
            recurring_text = driver.find_element(By.TAG_NAME, "body").text
            driver.save_screenshot(str(screenshot.with_name(screenshot.stem + "-recurring" + screenshot.suffix)))
            recurring_ratio = screenshot_content_ratio(screenshot.with_name(screenshot.stem + "-recurring" + screenshot.suffix))

            # Exercise the quick filter and edit UI bindings, not just the first render.
            filter_checks = {}
            driver.find_element(By.XPATH, "//button[normalize-space()='Subscriptions']").click()
            filter_checks["subscriptions_only"] = (
                len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="recurring-subscriptions-panel"]')) == 1
                and len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="recurring-other-panel"]')) == 0
            )
            driver.find_element(By.XPATH, "//button[normalize-space()='Bills']").click()
            filter_checks["bills_only"] = (
                len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="recurring-subscriptions-panel"]')) == 0
                and len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="recurring-other-panel"]')) == 1
            )
            driver.find_element(By.XPATH, "//button[normalize-space()='All']").click()
            filter_checks["all_sections"] = (
                len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="recurring-subscriptions-panel"]')) == 1
                and len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="recurring-other-panel"]')) == 1
            )

            logs = driver.get_log("browser")
            severe = [entry["message"] for entry in logs if entry.get("level") in {"SEVERE", "ERROR"}]
            vue_warnings = [entry["message"] for entry in logs if "[Vue warn]" in entry.get("message", "")]
            boot_screen_visible = driver.execute_script("return !!document.getElementById('boot-screen')")
            origin = urlparse(base_url).netloc
            script_urls = driver.execute_script("return Array.from(document.scripts).map((script) => script.src).filter(Boolean)")
            external_script_urls = [url for url in script_urls if urlparse(url).netloc != origin]

            checks = {
                "dashboard_sections": {item: item in dashboard_text for item in dashboard_sections},
                "recurring_sections": {item: item in recurring_text for item in recurring_sections},
                "api_statuses": api_statuses,
                "boot_screen_removed": not boot_screen_visible,
                "dashboard_screenshot_content": dashboard_ratio >= 0.003,
                "recurring_screenshot_content": recurring_ratio >= 0.003,
                "no_severe_browser_errors": not severe,
                "no_vue_warnings": not vue_warnings,
                "recurring_quick_filters": filter_checks,
                "no_external_runtime_scripts": not external_script_urls,
            }
            ok = (
                all(checks["dashboard_sections"].values())
                and all(checks["recurring_sections"].values())
                and all(status == 200 for status in api_statuses.values())
                and checks["boot_screen_removed"]
                and checks["dashboard_screenshot_content"]
                and checks["recurring_screenshot_content"]
                and checks["no_severe_browser_errors"]
                and checks["no_vue_warnings"]
                and all(filter_checks.values())
                and checks["no_external_runtime_scripts"]
            )
            print(json.dumps({
                "ok": ok,
                "mode": test_mode,
                "url": base_url,
                "title": driver.title,
                "dashboard_screenshot": str(screenshot),
                "dashboard_screenshot_content_ratio": round(dashboard_ratio, 4),
                "recurring_screenshot": str(screenshot.with_name(screenshot.stem + "-recurring" + screenshot.suffix)),
                "recurring_screenshot_content_ratio": round(recurring_ratio, 4),
                "checks": checks,
                "external_script_urls": external_script_urls,
                "severe_browser_errors": severe,
                "vue_warnings": vue_warnings,
            }, indent=2))
            if not ok:
                raise SystemExit(1)
        finally:
            driver.quit()
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=8)
        if test_data_dir is not None:
            test_data_dir.cleanup()


if __name__ == "__main__":
    main()
