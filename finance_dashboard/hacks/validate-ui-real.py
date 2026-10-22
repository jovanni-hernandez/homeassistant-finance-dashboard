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
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="frontend/index.html", help="Frontend file; used to locate the backend in local mode")
    configured_url = os.environ.get("FINANCE_DASHBOARD_URL")
    if configured_url == "local":
        configured_url = None
    parser.add_argument("--url", default=configured_url, help="Optional deployed app URL to test directly")
    parser.add_argument("--ha-ingress", action="store_true", help="Create an authenticated HA Ingress session using HA_URL/HA_TOKEN")
    parser.add_argument("--screenshot", default=str(artifact_dir() / "finance-dashboard-real-selenium.png"))
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--device", choices=("desktop", "iphone", "pixel"), default="desktop")
    return parser.parse_args()


def artifact_dir() -> Path:
    preferred = Path(os.environ.get("FINANCE_TEST_ARTIFACT_DIR", "/data/v2/cache/opencode"))
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        if os.access(preferred, os.W_OK):
            return preferred
    except OSError:
        pass
    fallback = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "opencode"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def resolve_ingress_target():
    """Create a short-lived Supervisor ingress session without exposing its token."""
    from ha_release import HAProxy, read_config

    ha_url = os.environ.get("HA_URL", "").rstrip("/")
    if not ha_url:
        raise RuntimeError("HA_URL is required for authenticated Ingress testing")
    client = HAProxy()
    slug, _ = read_config()
    app_info, _ = client.supervisor("GET", f"/addons/{slug}/info")
    ingress_entry = app_info.get("ingress_entry")
    if not isinstance(ingress_entry, str) or not ingress_entry.startswith("/api/hassio_ingress/"):
        raise RuntimeError("Supervisor returned no usable ingress_entry for the finance app")
    session_data, envelope = client.supervisor("POST", "/ingress/session", {})
    session = session_data.get("session") if isinstance(session_data, dict) else None
    if not session and isinstance(envelope, dict):
        session = (envelope.get("data") or {}).get("session")
    if not isinstance(session, str) or not session:
        raise RuntimeError("Supervisor returned no ingress session")
    parsed = urlparse(ha_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin, f"{origin}{ingress_entry.rstrip('/')}/", ingress_entry, session


def reserve_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_real_backend(frontend_file: Path):
    """Start the real FastAPI app with isolated DB/key and SimpleFIN mock fallback."""
    backend_dir = frontend_file.resolve().parent.parent / "backend"
    if not (backend_dir / "main.py").is_file():
        raise FileNotFoundError(f"Backend not found next to {frontend_file}: {backend_dir}")

    cache_root = artifact_dir()
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


def probe_api(base_url: str, headers: dict[str, str] | None = None) -> dict[str, int]:
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
        request_headers = {"Accept": "application/json", **(headers or {})}
        request = urllib.request.Request(base_url + endpoint, headers=request_headers)
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
    screenshot_base = Path(args.screenshot)
    suffix = screenshot_base.suffix or ".png"
    screenshot = screenshot_base.with_name(f"{screenshot_base.stem}-{args.device}{suffix}")
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    frontend_file = Path(args.file).resolve()
    server = thread = test_data_dir = None
    ingress_entry = None
    ingress_origin = None
    ingress_cookie = None

    try:
        if args.ha_ingress and args.url:
            raise RuntimeError("Choose --ha-ingress or --url, not both")
        if args.ha_ingress:
            ingress_origin, base_url, ingress_entry, ingress_cookie = resolve_ingress_target()
            test_mode = "deployed-ha-ingress"
        elif args.url:
            base_url = args.url.rstrip("/") + "/"
            test_mode = "deployed-url"
        else:
            base_url, server, thread, test_data_dir = start_real_backend(frontend_file)
            test_mode = "real-fastapi-isolated-db"

        request_headers = {}
        if ingress_cookie:
            request_headers["Cookie"] = f"ingress_session={ingress_cookie}"
        app_session = os.environ.get("FINANCE_SESSION_TOKEN", "")
        if app_session:
            request_headers["X-Finance-Session"] = app_session
        api_statuses = probe_api(base_url, request_headers)
        options = Options()
        options.binary_location = "/usr/bin/chromium"
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.set_capability("goog:loggingPrefs", {"browser": "ALL"})
        driver = webdriver.Chrome(options=options)

        try:
            viewport_sizes = {"desktop": (1280, 1000), "iphone": (390, 844), "pixel": (412, 915)}
            viewport_width, viewport_height = viewport_sizes[args.device]
            driver.set_window_size(viewport_width, viewport_height)
            driver.set_page_load_timeout(args.timeout)
            if test_mode == "real-fastapi-isolated-db":
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": r'''(() => {
                      const nativeFetch = window.fetch.bind(window);
                      window.fetch = async (...args) => {
                        const response = await nativeFetch(...args);
                        const url = typeof args[0] === "string" ? args[0] : args[0].url;
                        if (url.includes("/api/transactions")) {
                          const payload = await response.clone().json();
                          if (payload.transactions && payload.transactions.length) {
                            payload.transactions[0].description = "LONG-REFERENCE-" + "X".repeat(130) + " payment description should wrap instead of overflowing the card edge";
                          }
                          return new Response(JSON.stringify(payload), {status: response.status, headers: response.headers});
                        }
                        return response;
                      };
                    })();''',
                })
            if app_session:
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": "sessionStorage.setItem('finance-dashboard-session-token', " + json.dumps(app_session) + ");"
                })
            if ingress_cookie:
                # Set the transient Supervisor session cookie on the HA origin;
                # do not include its value or ingress token in diagnostics.
                driver.get(ingress_origin + "/")
                driver.add_cookie({"name": "ingress_session", "value": ingress_cookie, "path": "/"})
            driver.get(base_url)
            wait = WebDriverWait(driver, args.timeout)
            wait.until(lambda current: "Finance Dashboard" in current.title)
            wait.until(lambda current: current.execute_script("return !document.getElementById('boot-screen')"))
            wait.until(lambda current: "Loading financial data…" not in current.find_element(By.TAG_NAME, "body").text)

            dashboard_text = driver.find_element(By.TAG_NAME, "body").text
            dashboard_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
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
                        "mode": test_mode,
                        "body_length": len(current_text),
                        "browser_logs": driver.get_log("browser"),
                        "render_length_expressions": render_snippets,
                    }, indent=2))
                    raise RuntimeError(f"Recurring view did not render {section}") from exc
            recurring_text = driver.find_element(By.TAG_NAME, "body").text
            recurring_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
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

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.XPATH, "//button[.//span[text()='Dashboard']]").click()
            WebDriverWait(driver, args.timeout).until(lambda current: "Recent transactions" in current.find_element(By.TAG_NAME, "body").text)
            transaction_rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="transaction-card"]')
            if not transaction_rows:
                transaction_rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="transaction-row"]')
            show_more_buttons = [button for button in driver.find_elements(By.CSS_SELECTOR, '[data-testid="show-more-transactions"]') if button.is_displayed()]
            if show_more_buttons:
                initial_row_count = len(transaction_rows)
                show_more_buttons[0].click()
                visible_row_selector = '[data-testid="transaction-card"]' if args.device in {"iphone", "pixel"} else '[data-testid="transaction-row"]'
                WebDriverWait(driver, args.timeout).until(lambda current: len(current.find_elements(By.CSS_SELECTOR, visible_row_selector)) > initial_row_count)
                show_more_works = True
            else:
                show_more_works = len(transaction_rows) <= 15

            transaction_description_fits_card = True
            transaction_screenshot = None
            if args.device in {"iphone", "pixel"}:
                cards = driver.find_elements(By.CSS_SELECTOR, '[data-testid="transaction-card"]')
                if cards:
                    description = cards[0].find_element(By.CSS_SELECTOR, '[data-testid="transaction-description"]')
                    if test_mode == "real-fastapi-isolated-db":
                        transaction_description_fits_card = description.text.startswith("LONG-REFERENCE-")
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", cards[0])
                    layout = driver.execute_script("""const description=arguments[0], card=arguments[1];
                      return {
                        descriptionFits: description.scrollWidth <= description.clientWidth + 1,
                        cardFits: card.scrollWidth <= card.clientWidth + 1,
                        pageFits: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
                      };""", description, cards[0])
                    transaction_description_fits_card = transaction_description_fits_card and all(layout.values())
                    transaction_screenshot = screenshot.with_name(screenshot.stem + "-transactions" + screenshot.suffix)
                    driver.save_screenshot(str(transaction_screenshot))
                else:
                    transaction_description_fits_card = False

            sankey_interaction_checks = {
                "flow_click_shows_transactions": False,
                "node_click_shows_transactions": False,
                "node_keyboard_selection": False,
            }
            sankey_details_screenshot = screenshot.with_name(screenshot.stem + "-sankey-details" + screenshot.suffix)
            sankey_links = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-link"]')
            sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
            if sankey_links and sankey_nodes:
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", sankey_links[0])
                details_panel = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                sankey_interaction_checks["flow_click_shows_transactions"] = (
                    details_panel.get_attribute("data-sankey-selection-type") == "link"
                    and "→" in details_panel.text
                    and bool(details_panel.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]'))
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", details_panel)
                driver.save_screenshot(str(sankey_details_screenshot))

                sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", sankey_nodes[0])
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", sankey_nodes[0])
                node_panel = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                sankey_interaction_checks["node_click_shows_transactions"] = (
                    node_panel.get_attribute("data-sankey-selection-type") == "node"
                    and bool(node_panel.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]'))
                )

                sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                sankey_nodes[0].send_keys(Keys.ENTER)
                keyboard_panel = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                sankey_interaction_checks["node_keyboard_selection"] = (
                    keyboard_panel.get_attribute("data-sankey-selection-type") == "node"
                    and bool(keyboard_panel.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]'))
                )

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.XPATH, "//button[.//span[text()='Settings']]").click()
            WebDriverWait(driver, args.timeout).until(lambda current: "Settings" in current.find_element(By.TAG_NAME, "body").text)
            settings_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            driver.save_screenshot(str(screenshot.with_name(screenshot.stem + "-settings" + screenshot.suffix)))

            logs = driver.get_log("browser")
            def redact_log(message: str) -> str:
                if ingress_entry:
                    message = message.replace(ingress_entry, "/api/hassio_ingress/<redacted>")
                return message

            severe = [redact_log(entry["message"]) for entry in logs if entry.get("level") in {"SEVERE", "ERROR"}]
            vue_warnings = [redact_log(entry["message"]) for entry in logs if "[Vue warn]" in entry.get("message", "")]
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
                "sankey_interactions": sankey_interaction_checks,
                "transaction_description_fits_card": transaction_description_fits_card,
                "no_external_runtime_scripts": not external_script_urls,
                "dashboard_no_horizontal_overflow": not dashboard_overflow,
                "recurring_no_horizontal_overflow": not recurring_overflow,
                "settings_no_horizontal_overflow": not settings_overflow,
                "show_more_transactions_works": show_more_works,
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
                and all(sankey_interaction_checks.values())
                and transaction_description_fits_card
                and checks["no_external_runtime_scripts"]
                and checks["dashboard_no_horizontal_overflow"]
                and checks["recurring_no_horizontal_overflow"]
                and checks["settings_no_horizontal_overflow"]
                and checks["show_more_transactions_works"]
            )
            print(json.dumps({
                "ok": ok,
                "mode": test_mode,
                "device": args.device,
                "url": "HA Ingress (session/entry redacted)" if ingress_entry else base_url,
                "title": driver.title,
                "dashboard_screenshot": str(screenshot),
                "dashboard_screenshot_content_ratio": round(dashboard_ratio, 4),
                "recurring_screenshot": str(screenshot.with_name(screenshot.stem + "-recurring" + screenshot.suffix)),
                "recurring_screenshot_content_ratio": round(recurring_ratio, 4),
                "sankey_details_screenshot": str(sankey_details_screenshot),
                "transaction_screenshot": str(transaction_screenshot) if transaction_screenshot else None,
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
