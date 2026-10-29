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
from selenium.webdriver.support.ui import Select, WebDriverWait


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
    source_frontend = frontend_file.resolve().parent
    source_backend = source_frontend.parent / "backend"
    if not (source_backend / "main.py").is_file():
        raise FileNotFoundError(f"Backend not found next to {frontend_file}: {source_backend}")

    cache_root = artifact_dir()
    test_data_dir = tempfile.TemporaryDirectory(prefix="finance-dashboard-e2e-", dir=cache_root)
    backend_dir = source_backend
    os.environ["FINANCE_DB_PATH"] = str(Path(test_data_dir.name) / "test.sqlite")
    os.environ["FINANCE_KEY_PATH"] = str(Path(test_data_dir.name) / "test.key")
    os.environ["FINANCE_AGENT_STDIN_ENABLED"] = "0"
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

    startup_timeout = max(45, int(os.environ.get("FINANCE_TEST_BACKEND_STARTUP_TIMEOUT", "120")))
    deadline = time.monotonic() + startup_timeout
    last_error = None
    while time.monotonic() < deadline:
        if not thread.is_alive():
            test_data_dir.cleanup()
            raise RuntimeError("Real FastAPI test server exited during startup")
        try:
            with urllib.request.urlopen(base_url + "api/pin/status", timeout=2) as response:
                if response.status == 200:
                    break
        except Exception as exc:  # server may still be starting its DB and mock sync
            last_error = exc
            time.sleep(0.25)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        test_data_dir.cleanup()
        raise TimeoutError(f"Real FastAPI app did not become ready: {last_error}")

    return base_url, server, thread, test_data_dir


def seed_existing_category_match_for_ui_test():
    """Add one old categorized merchant match to the disposable browser-test DB."""
    from models import SessionLocal, Transaction

    db = SessionLocal()
    try:
        if db.query(Transaction).filter(Transaction.id == "test-old-coffee-match").first():
            return
        db.add(Transaction(
            id="test-old-coffee-match",
            account_id="1",
            date=1718300000,
            amount="-8.00",
            payee="Coffee Shop",
            description="Previous coffee stop",
            category="Dining",
            pending=False,
        ))
        db.commit()
    finally:
        db.close()


def probe_api(base_url: str, headers: dict[str, str] | None = None) -> dict[str, int]:
    endpoints = [
        "api/status",
        "api/pin/status",
        "api/accounts",
        "api/manual-assets",
        "api/goals",
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


def probe_index_cache_policy(base_url: str, headers: dict[str, str] | None = None) -> bool:
    request = urllib.request.Request(base_url, headers={"Accept": "text/html", **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        cache_control = response.headers.get("Cache-Control", "").lower()
        return response.status == 200 and "no-store" in cache_control


def screenshot_content_ratio(path: Path) -> float:
    image = Image.open(path).convert("L")
    values = image.tobytes()
    return sum(value < 245 for value in values) / max(1, len(values))


def sankey_mobile_layout_metrics(driver) -> dict:
    return driver.execute_script("""
        const container = document.querySelector('[data-testid="sankey-chart"]');
        const svg = container?.querySelector('svg');
        if (!container || !svg) return { ready: false };
        const labels = Array.from(svg.querySelectorAll('[data-testid="sankey-node"] text'));
        const boxes = labels.map((element) => {
            const box = element.getBBox();
            return {
                element,
                x: box.x,
                y: box.y,
                right: box.x + box.width,
                bottom: box.y + box.height,
            };
        });
        let labelOverlaps = 0;
        for (let i = 0; i < boxes.length; i += 1) {
            for (let j = i + 1; j < boxes.length; j += 1) {
                const a = boxes[i];
                const b = boxes[j];
                if (Math.min(a.right, b.right) - Math.max(a.x, b.x) > 1
                    && Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 1) labelOverlaps += 1;
            }
        }
        const viewBox = svg.viewBox.baseVal;
        const clippedLabels = boxes.filter((box) => (
            box.x < -1 || box.y < -1 || box.right > viewBox.width + 1 || box.bottom > viewBox.height + 1
        )).length;
        const rightmostLabel = labels.reduce((rightmost, candidate) => (
            !rightmost || candidate.getBBox().x + candidate.getBBox().width
                > rightmost.getBBox().x + rightmost.getBBox().width
                ? candidate : rightmost
        ), null);
        const originalScrollLeft = container.scrollLeft;
        container.scrollLeft = container.scrollWidth;
        const containerRect = container.getBoundingClientRect();
        const rightmostRect = rightmostLabel?.getBoundingClientRect();
        const rightmostLabelReachable = !!rightmostRect
            && rightmostRect.left < containerRect.right
            && rightmostRect.right > containerRect.left;
        container.scrollLeft = originalScrollLeft;
        const nodeRects = Array.from(svg.querySelectorAll('[data-testid="sankey-node"] rect'));
        let labelNodeOverlaps = 0;
        boxes.forEach((box) => {
            const ownNode = box.element.closest('[data-testid="sankey-node"]');
            nodeRects.forEach((rect) => {
                if (rect.closest('[data-testid="sankey-node"]') === ownNode) return;
                const other = rect.getBBox();
                if (Math.min(box.right, other.x + other.width) - Math.max(box.x, other.x) > 1
                    && Math.min(box.bottom, other.y + other.height) - Math.max(box.y, other.y) > 1) labelNodeOverlaps += 1;
            });
        });
        return {
            ready: labels.length > 0,
            containerWidth: container.clientWidth,
            containerHeight: container.clientHeight,
            scrollWidth: container.scrollWidth,
            svgWidth: viewBox.width,
            svgHeight: viewBox.height,
            renderedSvgHeight: svg.getBoundingClientRect().height,
            labelCount: labels.length,
            labelOverlaps,
            labelNodeOverlaps,
            clippedLabels,
            rightmostLabelReachable,
        };
    """)


def sankey_crossing_order_metrics(driver) -> dict:
    return driver.execute_script("""
        const orderNodes = window.__financeSankeyNodeOrderByTransactionCount;
        if (typeof orderNodes !== 'function' || !window.d3?.sankey) return {
            ready: false,
            helperAvailable: typeof orderNodes === 'function',
            sankeyLibraryAvailable: typeof window.d3?.sankey === 'function',
        };

        const accountCount = 8;
        const categoryCount = 10;
        const nodes = [{ name: 'Paycheck deposits' }];
        for (let i = 0; i < accountCount; i += 1) nodes.push({ name: `Account ${i}` });
        for (let i = 0; i < categoryCount; i += 1) nodes.push({ name: `Category ${i}` });
        const linksByNodes = new Map();
        const addLink = (source, target, value) => {
            const key = `${source}:${target}`;
            let link = linksByNodes.get(key);
            if (!link) {
                link = { source, target, value: 0, transactions: [] };
                linksByNodes.set(key, link);
            }
            link.value += value;
            link.transactions.push({});
        };
        for (let i = 0; i < accountCount; i += 1) {
            addLink(0, 1 + i, 1000 - i * 30);
        }
        for (let account = 0; account < accountCount; account += 1) {
            for (let category = 0; category < categoryCount; category += 1) {
                const count = (account * 17 + category * 23 + 11) % 5 === 0
                    ? 0
                    : ((account * 13 + category * 19 + 7) % 4 + 1);
                for (let item = 0; item < count; item += 1) {
                    addLink(
                        1 + account,
                        1 + accountCount + category,
                        (account * 31 + category * 11 + item * 7) % 90 + 1,
                    );
                }
            }
        }
        const links = Array.from(linksByNodes.values());
        const data = () => ({
            nodes: nodes.map((node, layoutId) => ({ ...node, layoutId })),
            links: links.map((link) => ({ ...link })),
        });
        const layout = (chartHeight, nodeSort) => {
            let sankey = d3.sankey()
                .nodeWidth(14)
                .nodePadding(18)
                .extent([[10, 10], [630, chartHeight - 10]])
                .iterations(6);
            if (nodeSort) sankey = sankey.nodeSort(nodeSort);
            return sankey(data());
        };
        const crossingCount = (graph) => {
            const layers = new Map();
            graph.links.forEach((link) => {
                const key = `${link.source.depth}:${link.target.depth}`;
                if (!layers.has(key)) layers.set(key, []);
                layers.get(key).push(link);
            });
            let crossings = 0;
            layers.forEach((linksInLayer) => {
                for (let i = 0; i < linksInLayer.length; i += 1) {
                    for (let j = i + 1; j < linksInLayer.length; j += 1) {
                        const left = linksInLayer[i];
                        const right = linksInLayer[j];
                        if ((left.y0 - right.y0) * (left.y1 - right.y1) < -0.01) crossings += 1;
                    }
                }
            });
            return crossings;
        };
        const baseline = layout(380, null);
        const probe = layout(780, null);
        const order = orderNodes(probe);
        const ordered = layout(380, (left, right) => order.get(left.layoutId) - order.get(right.layoutId));
        return {
            ready: true,
            linkCount: links.length,
            transactionCount: links.reduce((count, link) => count + link.transactions.length, 0),
            baselineCrossings: crossingCount(baseline),
            orderedCrossings: crossingCount(ordered),
        };
    """)


def main():
    args = parse_args()
    screenshot_base = Path(args.screenshot)
    suffix = screenshot_base.suffix or ".png"
    screenshot = screenshot_base.with_name(f"{screenshot_base.stem}-{args.device}{suffix}")
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    frontend_file = Path(args.file).resolve()
    server = thread = test_data_dir = None
    browser_profile_dir = None
    driver = None
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
            seed_existing_category_match_for_ui_test()

        request_headers = {}
        if ingress_cookie:
            request_headers["Cookie"] = f"ingress_session={ingress_cookie}"
        app_session = os.environ.get("FINANCE_SESSION_TOKEN", "")
        if app_session:
            request_headers["X-Finance-Session"] = app_session
        api_statuses = probe_api(base_url, request_headers)
        index_html_no_store_cache = probe_index_cache_policy(base_url, request_headers)
        options = Options()
        options.binary_location = "/usr/bin/chromium"
        options.page_load_strategy = "none"
        browser_profile_dir = tempfile.TemporaryDirectory(
            prefix="finance-dashboard-chrome-",
            dir=artifact_dir(),
        )
        options.add_argument(f"--user-data-dir={browser_profile_dir.name}")
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
                    "source": "window.__FINANCE_TESTING__ = true;",
                })
            inject_test_description = (
                test_mode == "real-fastapi-isolated-db"
                and os.environ.get("FINANCE_TEST_LONG_DESCRIPTION", "1") != "0"
            )
            if inject_test_description:
                driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                    "source": r'''(() => {
                      const nativeFetch = window.fetch.bind(window);
                      let injectedSourceTransaction = null;
                      window.fetch = async (...args) => {
                        const url = typeof args[0] === "string" ? args[0] : args[0].url;
                        if (url.endsWith("/api/ai/chat")) {
                          return new Response(JSON.stringify({
                            reply: "A savings goal draft can help turn that target into a plan.",
                            goal_draft: {goal_type: "savings", name: "Emergency fund", target_amount: "10000", target_date: "2030-12-31"}
                          }), {status: 200, headers: {"Content-Type": "application/json"}});
                        }
                        const response = await nativeFetch(...args);
                        if (url.endsWith("/api/ai/settings")) {
                          const payload = await response.clone().json();
                          payload.enabled = true;
                          payload.openai_key_set = true;
                          const headers = new Headers(response.headers);
                          headers.delete("content-length");
                          headers.delete("content-encoding");
                          return new Response(JSON.stringify(payload), {status: response.status, headers});
                        }
                        if (url.includes("/api/accounts")) {
                          const payload = await response.clone().json();
                          payload.accounts = [
                            ...(payload.accounts || []),
                            {id: "test-account-checking-main", name: "Checking", org: "Test Bank", balance: "1000.00", currency: "USD"},
                            {id: "test-account-savings-main", name: "Savings", org: "Test Bank", balance: "2000.00", currency: "USD"},
                            {id: "test-account-checking-joint", name: "Checking", org: "Test Credit Union", balance: "500.00", currency: "USD"}
                          ];
                          const headers = new Headers(response.headers);
                          headers.delete("content-length");
                          headers.delete("content-encoding");
                          return new Response(JSON.stringify(payload), {status: response.status, headers});
                        }
                        if (url.includes("/api/transactions")) {
                          const payload = await response.clone().json();
                          const requestUrl = new URL(url, window.location.href);
                          const offset = Number(requestUrl.searchParams.get("offset") || 0);
                          if (offset === 500 && injectedSourceTransaction) {
                            const source = injectedSourceTransaction;
                            payload.transactions = [{
                              id: "test-rosegate-mortgage",
                              account_id: source.account_id,
                              account_name: source.account_name,
                              account_nickname: source.account_nickname,
                              org: source.org,
                              date: source.date,
                              amount: "-3100.00",
                              payee: "Rosegate",
                              description: "Mortgage transfer payment",
                              category: "Housing",
                              pending: false
                            }];
                            payload.total = 501;
                            payload.offset = 500;
                          } else if (offset === 0 && payload.transactions && payload.transactions.length) {
                            payload.transactions[0].description = "LONG-REFERENCE-" + "X".repeat(130) + " payment description should wrap instead of overflowing the card edge";
                            payload.transactions[0].pending = true;
                            if (payload.transactions[0].category === "Uncategorized") {
                              payload.transactions[0].category = "Groceries";
                            }
                            const now = Number(payload.transactions[0].date || Math.floor(Date.now() / 1000));
                            const transfer = (id, account_name, account_id, amount, offsetSeconds) => ({
                              id, account_name, account_id, amount, date: now + offsetSeconds,
                              org: account_id === "test-account-checking-joint" ? "Test Credit Union" : "Test Bank",
                              payee: "Internal Transfer", description: "Internal transfer between linked accounts",
                              category: "Transfers", pending: false
                            });
                            payload.transactions.push(
                              transfer("test-transfer-checking-savings-out", "Checking", "test-account-checking-main", -1234.56, -30),
                              transfer("test-transfer-checking-savings-in", "Savings", "test-account-savings-main", 1234.56, 30),
                              transfer("test-transfer-checking-checking-out", "Checking", "test-account-checking-joint", -789.01, -20),
                              transfer("test-transfer-checking-checking-in", "Checking", "test-account-checking-main", 789.01, 20),
                              transfer("test-transfer-savings-checking-out", "Savings", "test-account-savings-main", -2468.02, -10),
                              transfer("test-transfer-savings-checking-in", "Checking", "test-account-checking-main", 2468.02, 10),
                              transfer("test-transfer-unmatched-out", "Checking", "test-account-checking-main", -321.09, -5)
                            );
                            injectedSourceTransaction = payload.transactions[0];
                            while (payload.transactions.length < 500) {
                              const sequence = payload.transactions.length;
                              payload.transactions.push({
                                id: `test-pagination-filler-${sequence}`,
                                account_id: injectedSourceTransaction.account_id,
                                account_name: injectedSourceTransaction.account_name,
                                account_nickname: injectedSourceTransaction.account_nickname,
                                org: injectedSourceTransaction.org,
                                date: now - 400 * 86400 - sequence,
                                amount: "0.00",
                                payee: "Pagination fixture",
                                description: "Old zero-value test row",
                                category: "Uncategorized",
                                pending: false
                              });
                            }
                            payload.total = 501;
                            payload.offset = 0;
                          }
                          const headers = new Headers(response.headers);
                          headers.delete("content-length");
                          headers.delete("content-encoding");
                          return new Response(JSON.stringify(payload), {status: response.status, headers});
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
            if test_mode == "real-fastapi-isolated-db":
                sankey_crossing_metrics = sankey_crossing_order_metrics(driver)
                sankey_crossing_check = (
                    sankey_crossing_metrics.get("ready")
                    and sankey_crossing_metrics.get("orderedCrossings", float("inf"))
                        < sankey_crossing_metrics.get("baselineCrossings", 0)
                )
                sankey_link_aggregation_check = (
                    sankey_crossing_metrics.get("ready")
                    and sankey_crossing_metrics.get("linkCount", 0)
                        < sankey_crossing_metrics.get("transactionCount", 0)
                )
            else:
                sankey_crossing_metrics = {"ready": True, "check": "local fixture only"}
                sankey_crossing_check = True
                sankey_link_aggregation_check = True

            dashboard_text = driver.find_element(By.TAG_NAME, "body").text
            dashboard_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            render_snippets = driver.execute_script(
                "const app=document.querySelector('#app')?.__vue_app__;"
                "const text=(app?app._instance?.render?.toString():'')||'';"
                "return [...text.matchAll(/.{0,100}length.{0,100}/g)].map(match=>match[0]).slice(0,30);"
            )
            dashboard_sections = ["Cash flow", "Spending by category", "Accounts", "Recent transactions"]
            for section in dashboard_sections:
                wait.until(lambda current, item=section: item in current.find_element(By.TAG_NAME, "body").text)
            driver.save_screenshot(str(screenshot))
            dashboard_ratio = screenshot_content_ratio(screenshot)
            driver.execute_script("window.scrollTo(0, document.documentElement.scrollHeight)")
            header_scroll_checks = driver.execute_script("""
                const compactBar = document.querySelector('#app > div.sticky');
                const detailsHeader = document.querySelector('#app > header');
                const compactRect = compactBar?.getBoundingClientRect();
                const detailsRect = detailsHeader?.getBoundingClientRect();
                return {
                    compact_bar_stays_pinned: Boolean(compactRect && Math.abs(compactRect.top) <= 1),
                    header_details_scroll_away: Boolean(compactRect && detailsRect && detailsRect.bottom <= compactRect.bottom),
                };
            """)
            driver.execute_script("window.scrollTo(0, 0)")

            theme_checks = {
                "dark_ha_theme_detected": True,
                "light_ha_theme_detected": True,
            }
            dark_mode_screenshot = None
            if test_mode == "real-fastapi-isolated-db":
                original_primary_background = driver.execute_script(
                    "return document.documentElement.style.getPropertyValue('--primary-background-color');"
                )
                driver.execute_script("document.documentElement.style.setProperty('--primary-background-color', '#111111');")
                WebDriverWait(driver, args.timeout).until(
                    lambda current: "finance-dark-mode" in current.find_element(By.TAG_NAME, "html").get_attribute("class")
                )
                theme_checks["dark_ha_theme_detected"] = driver.execute_script(
                    "return document.documentElement.dataset.financeTheme === 'dark';"
                )
                dark_mode_screenshot = screenshot.with_name(screenshot.stem + "-dark-mode" + screenshot.suffix)
                driver.save_screenshot(str(dark_mode_screenshot))
                driver.execute_script("document.documentElement.style.setProperty('--primary-background-color', '#fafafa');")
                WebDriverWait(driver, args.timeout).until(
                    lambda current: "finance-dark-mode" not in current.find_element(By.TAG_NAME, "html").get_attribute("class")
                )
                theme_checks["light_ha_theme_detected"] = driver.execute_script(
                    "return document.documentElement.dataset.financeTheme === 'light';"
                )
                if original_primary_background:
                    driver.execute_script("document.documentElement.style.setProperty('--primary-background-color', arguments[0]);", original_primary_background)
                else:
                    driver.execute_script("document.documentElement.style.removeProperty('--primary-background-color');")

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
            transaction_row_selector = '[data-testid="transaction-row"]'
            transaction_rows = driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            group_headings = driver.find_elements(By.CSS_SELECTOR, '[data-testid="transaction-group-heading"]')
            group_types = [group.get_attribute("data-group-type") for group in driver.find_elements(By.CSS_SELECTOR, '[data-testid="transaction-group"]')]
            transaction_group_checks = {
                "pending_group_first": bool(group_types) and group_types[0] == "pending" and group_headings[0].text == "Pending",
                "posted_transactions_grouped_by_date": "date" in group_types,
            }
            show_more_buttons = [button for button in driver.find_elements(By.CSS_SELECTOR, '[data-testid="show-more-transactions"]') if button.is_displayed()]
            if show_more_buttons:
                initial_row_count = len(transaction_rows)
                show_more_buttons[0].click()
                WebDriverWait(driver, args.timeout).until(lambda current: len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) > initial_row_count)
                show_more_works = True
            else:
                show_more_works = len(transaction_rows) <= 15

            transaction_description_fits_card = True
            transaction_filter_checks = {
                "search_matches_merchant": False,
                "category_filter_matches": False,
                "empty_state_for_no_match": False,
                "manual_category_edit_works": False,
                "manual_category_change_prompts_for_future_rule": False,
                "one_off_choice_does_not_create_rule": False,
                "future_rule_choice_saves_selected_patterns": False,
                "future_rule_choice_preserves_existing_defaults": False,
                "category_rule_match_source_is_selectable": False,
                "description_source_recalculates_existing_matches": False,
                "retroactive_prompt_shows_count_and_warning": False,
                "retroactive_choice_updates_all_matching_entries": False,
                "account_cards_toggle_and_multi_select": False,
                "account_filtered_phone_layout_has_no_overflow": True,
            }
            spending_pie_checks = {
                "transfers_excluded_by_default": False,
                "toggle_adds_transfer_slice": False,
                "category_slice_filters_and_reselect_clears": False,
                "pie_category_filters_sankey": False,
                "clearing_pie_filter_restores_sankey": False,
                "transfer_slice_filters_transfer_entries": False,
                "phone_layout_has_no_horizontal_overflow": True,
                "mortgage_payment_in_housing_slice": False,
            }
            transaction_screenshot = None
            category_rule_prompt_screenshot = None
            category_rule_existing_prompt_screenshot = None
            sankey_filtered_screenshot = None
            sankey_restored_screenshot = None
            account_filter_screenshot = None
            spending_pie_screenshot = None
            if args.device in {"iphone", "pixel"}:
                rows = driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
                if rows:
                    description = rows[0].find_element(By.CSS_SELECTOR, '[data-testid="transaction-description"]')
                    if inject_test_description:
                        transaction_description_fits_card = description.text.startswith("LONG-REFERENCE-")
                    transaction_search = driver.find_element(By.CSS_SELECTOR, '[data-testid="transaction-search"]')
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", transaction_search)
                    layout = driver.execute_script("""const description=arguments[0], row=arguments[1];
                      const style=getComputedStyle(description);
                      return {
                        descriptionHasCompactOverflow: description.clientWidth > 0 && style.textOverflow === 'ellipsis' && style.whiteSpace === 'nowrap',
                        rowFits: row.scrollWidth <= row.clientWidth + 1,
                        pageFits: document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1
                      };""", description, rows[0])
                    transaction_description_fits_card = transaction_description_fits_card and all(layout.values())
                    transaction_screenshot = screenshot.with_name(screenshot.stem + "-transactions" + screenshot.suffix)
                    driver.save_screenshot(str(transaction_screenshot))
                else:
                    transaction_description_fits_card = False

            search_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="transaction-search"]')
            search_target = driver.find_element(By.CSS_SELECTOR, f'{transaction_row_selector} [data-testid="transaction-merchant"]').text
            search_input.send_keys(search_target)
            WebDriverWait(driver, args.timeout).until(
                lambda current: len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) >= 1
            )
            transaction_filter_checks["search_matches_merchant"] = any(
                search_target in row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-merchant"]').text
                for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            )
            driver.execute_script("""const input=arguments[0];
              const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              setter.call(input, '');
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.dispatchEvent(new Event('change', {bubbles:true}));""", search_input)
            WebDriverWait(driver, args.timeout).until(lambda current: search_input.get_attribute("value") == "")

            category_filter = Select(driver.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]'))
            category_filter.select_by_value("Groceries")
            WebDriverWait(driver, args.timeout).until(
                lambda current: len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) >= 1
            )
            transaction_filter_checks["category_filter_matches"] = all(
                row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]').get_attribute("value") == "Groceries"
                for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            )
            category_filter.select_by_value("Childcare")
            WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="transaction-empty"]'))
            )
            transaction_filter_checks["empty_state_for_no_match"] = True
            category_filter.select_by_value("all")
            WebDriverWait(driver, args.timeout).until(
                lambda current: len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) >= 1
            )
            checking_id = "test-account-checking-main"
            savings_id = "test-account-savings-main"
            checking_card = driver.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{checking_id}"]')
            savings_card = driver.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{savings_id}"]')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checking_card)
            checking_card.click()
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{checking_id}"]').get_attribute("aria-pressed") == "true"
                and len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) == 4
            )
            checking_rows = driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            checking_only = all(row.get_attribute("data-account-id") == checking_id for row in checking_rows)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", savings_card)
            savings_card.click()
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{savings_id}"]').get_attribute("aria-pressed") == "true"
                and len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) == 6
            )
            combined_rows = driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            combined_ids = {row.get_attribute("data-account-id") for row in combined_rows}
            multi_account_filter = combined_ids == {checking_id, savings_id}
            if args.device in {"iphone", "pixel"}:
                account_filter_screenshot = screenshot.with_name(screenshot.stem + "-account-filter" + screenshot.suffix)
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", driver.find_element(By.CSS_SELECTOR, '[data-testid="transaction-search"]'))
                driver.save_screenshot(str(account_filter_screenshot))
                transaction_filter_checks["account_filtered_phone_layout_has_no_overflow"] = driver.execute_script(
                    "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
                )
            checking_card = driver.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{checking_id}"]')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", checking_card)
            checking_card.click()
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{checking_id}"]').get_attribute("aria-pressed") == "false"
                and len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) == 2
            )
            savings_only_rows = driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            savings_only = all(row.get_attribute("data-account-id") == savings_id for row in savings_only_rows)
            savings_card = driver.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{savings_id}"]')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", savings_card)
            savings_card.click()
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{savings_id}"]').get_attribute("aria-pressed") == "false"
                and "accounts selected" not in current.find_element(By.TAG_NAME, "body").text
            )
            transaction_filter_checks["account_cards_toggle_and_multi_select"] = (
                checking_only and multi_account_filter and savings_only
            )

            spending_pie_section = driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-category-section"]')
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", spending_pie_section)
            pie_slices = driver.find_elements(By.CSS_SELECTOR, '[data-testid="spending-pie-slice"]')
            default_pie_categories = {slice_.get_attribute("data-category") for slice_ in pie_slices}
            spending_pie_checks["transfers_excluded_by_default"] = (
                bool(default_pie_categories)
                and "Transfers" not in default_pie_categories
                and driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-transfers-toggle"]').get_attribute("aria-pressed") == "false"
            )
            spending_pie_checks["mortgage_payment_in_housing_slice"] = "Housing" in default_pie_categories
            if args.device in {"iphone", "pixel"}:
                spending_pie_screenshot = screenshot.with_name(screenshot.stem + "-spending-pie" + screenshot.suffix)
                driver.save_screenshot(str(spending_pie_screenshot))
                spending_pie_checks["phone_layout_has_no_horizontal_overflow"] = driver.execute_script(
                    "return document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
                )

            transfer_toggle = driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-transfers-toggle"]')
            driver.execute_script("arguments[0].click();", transfer_toggle)
            WebDriverWait(driver, args.timeout).until(
                lambda current: any(
                    slice_.get_attribute("data-category") == "Transfers"
                    for slice_ in current.find_elements(By.CSS_SELECTOR, '[data-testid="spending-pie-slice"]')
                )
            )
            spending_pie_checks["toggle_adds_transfer_slice"] = (
                driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-transfers-toggle"]').get_attribute("aria-pressed") == "true"
            )

            transfer_slice = driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-pie-slice"][data-category="Transfers"]')
            driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", transfer_slice)
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == "Transfers"
                and len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) > 0
            )
            spending_pie_checks["transfer_slice_filters_transfer_entries"] = all(
                "Internal Transfer" in row.text
                for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            )
            transfer_slice = driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-pie-slice"][data-category="Transfers"]')
            driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", transfer_slice)
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == "all"
            )
            driver.execute_script("arguments[0].click();", driver.find_element(By.CSS_SELECTOR, '[data-testid="spending-transfers-toggle"]'))
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="spending-transfers-toggle"]').get_attribute("aria-pressed") == "false"
            )

            category_to_test = "Groceries" if "Groceries" in default_pie_categories else sorted(default_pie_categories)[0]
            driver.execute_script("arguments[0].click();", driver.find_element(By.CSS_SELECTOR, '[data-testid="sankey-cashflow-tab"]'))
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="sankey-cashflow-tab"]').get_attribute("aria-selected") == "true"
            )

            def current_sankey_node_names(current):
                return {
                    node.get_attribute("data-sankey-node-name")
                    for node in current.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                }

            full_sankey_node_names = current_sankey_node_names(driver)
            category_slice = driver.find_element(By.CSS_SELECTOR, f'[data-testid="spending-pie-slice"][data-category="{category_to_test}"]')
            driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", category_slice)
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == category_to_test
                and len(current.find_elements(By.CSS_SELECTOR, transaction_row_selector)) > 0
            )
            category_rows_match = all(
                row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]').get_attribute("value") == category_to_test
                for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
            )
            WebDriverWait(driver, args.timeout).until(
                lambda current: (
                    category_to_test in current_sankey_node_names(current)
                    and current_sankey_node_names(current) != full_sankey_node_names
                )
            )
            filtered_sankey_node_names = current_sankey_node_names(driver)
            spending_pie_checks["pie_category_filters_sankey"] = (
                category_to_test in filtered_sankey_node_names
                and "Housing" not in filtered_sankey_node_names
            )
            sankey_filtered_screenshot = screenshot.with_name(screenshot.stem + "-sankey-pie-filtered" + screenshot.suffix)
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                driver.find_element(By.CSS_SELECTOR, '[data-testid="sankey-chart"]'),
            )
            driver.save_screenshot(str(sankey_filtered_screenshot))
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", spending_pie_section)
            full_breakdown_remains = len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="spending-pie-slice"]')) == len(default_pie_categories)
            category_slice = driver.find_element(By.CSS_SELECTOR, f'[data-testid="spending-pie-slice"][data-category="{category_to_test}"]')
            driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", category_slice)
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == "all"
                and current_sankey_node_names(current) == full_sankey_node_names
            )
            spending_pie_checks["category_slice_filters_and_reselect_clears"] = category_rows_match and full_breakdown_remains
            spending_pie_checks["clearing_pie_filter_restores_sankey"] = current_sankey_node_names(driver) == full_sankey_node_names
            sankey_restored_screenshot = screenshot.with_name(screenshot.stem + "-sankey-pie-reset" + screenshot.suffix)
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                driver.find_element(By.CSS_SELECTOR, '[data-testid="sankey-chart"]'),
            )
            driver.save_screenshot(str(sankey_restored_screenshot))

            sankey_layout_checks = {
                "cashflow_has_readable_mobile_width": True,
                "cashflow_right_labels_reachable_by_swipe": True,
                "cashflow_labels_do_not_overlap_or_clip": True,
                "cashflow_container_matches_svg_height": True,
                "transfers_has_readable_mobile_width": True,
                "transfers_right_labels_reachable_by_swipe": True,
                "transfers_labels_do_not_overlap_or_clip": True,
                "transfers_container_matches_svg_height": True,
            }
            sankey_mobile_measurements = {"cashflow": None, "transfers": None}
            if args.device in {"iphone", "pixel"}:
                WebDriverWait(driver, args.timeout).until(
                    lambda current: sankey_mobile_layout_metrics(current).get("ready")
                )
                cashflow_metrics = sankey_mobile_layout_metrics(driver)
                sankey_mobile_measurements["cashflow"] = cashflow_metrics
                sankey_layout_checks["cashflow_has_readable_mobile_width"] = (
                    cashflow_metrics["svgWidth"] >= 640
                    and cashflow_metrics["scrollWidth"] > cashflow_metrics["containerWidth"] + 1
                )
                sankey_layout_checks["cashflow_right_labels_reachable_by_swipe"] = cashflow_metrics[
                    "rightmostLabelReachable"
                ]
                sankey_layout_checks["cashflow_labels_do_not_overlap_or_clip"] = (
                    cashflow_metrics["labelOverlaps"] == 0
                    and cashflow_metrics["labelNodeOverlaps"] == 0
                    and cashflow_metrics["clippedLabels"] == 0
                )
                sankey_layout_checks["cashflow_container_matches_svg_height"] = abs(
                    cashflow_metrics["containerHeight"] - cashflow_metrics["renderedSvgHeight"]
                ) <= 1

            sankey_interaction_checks = {
                "flow_click_shows_transactions": False,
                "repeated_account_category_transactions_share_link": False,
                "category_flow_filters_main_list_and_pie": False,
                "category_node_and_pie_share_filter": False,
                "node_click_shows_transactions": False,
                "node_keyboard_selection": False,
                "cashflow_excludes_internal_transfers": False,
                "linked_transfers_match_across_account_types": False,
                "reverse_transfers_render_without_cycles": False,
                "unmatched_transfer_entries_remain_visible": False,
                "paired_transfer_details_show_both_entries": False,
                "mortgage_payment_in_cashflow_sankey": False,
            }
            sankey_details_screenshot = screenshot.with_name(screenshot.stem + "-sankey-details" + screenshot.suffix)
            transfer_screenshot = screenshot.with_name(screenshot.stem + "-transfers" + screenshot.suffix)
            sankey_links = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-link"]')
            sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
            sankey_interaction_checks["mortgage_payment_in_cashflow_sankey"] = any(
                node.get_attribute("data-sankey-node-name") == "Housing" for node in sankey_nodes
            )
            if sankey_links and sankey_nodes:
                full_cashflow_node_names = current_sankey_node_names(driver)
                category_flow = next(
                    (
                        link for link in sankey_links
                        if link.get_attribute("data-sankey-target-type") == "category"
                        and link.get_attribute("data-sankey-target-name") == category_to_test
                    ),
                    None,
                )
                if category_flow:
                    driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", category_flow)
                    WebDriverWait(driver, args.timeout).until(
                        lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == category_to_test
                        and current_sankey_node_names(current) != full_cashflow_node_names
                    )
                    pie_legend = next(
                        button for button in driver.find_elements(By.CSS_SELECTOR, '[data-testid="spending-category-legend-item"]')
                        if button.get_attribute("data-category") == category_to_test
                    )
                    filtered_rows_are_category = all(
                        row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]').get_attribute("value") == category_to_test
                        for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
                    )
                    sankey_interaction_checks["category_flow_filters_main_list_and_pie"] = (
                        filtered_rows_are_category
                        and pie_legend.get_attribute("aria-pressed") == "true"
                        and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-details"]')
                    )

                    category_node = next(
                        node for node in driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                        if node.get_attribute("data-sankey-node-type") == "category"
                        and node.get_attribute("data-sankey-node-name") == category_to_test
                    )
                    driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", category_node)
                    WebDriverWait(driver, args.timeout).until(
                        lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == "all"
                        and current_sankey_node_names(current) == full_cashflow_node_names
                    )
                    category_node = next(
                        node for node in driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                        if node.get_attribute("data-sankey-node-type") == "category"
                        and node.get_attribute("data-sankey-node-name") == category_to_test
                    )
                    driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", category_node)
                    WebDriverWait(driver, args.timeout).until(
                        lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == category_to_test
                    )
                    pie_legend = next(
                        button for button in driver.find_elements(By.CSS_SELECTOR, '[data-testid="spending-category-legend-item"]')
                        if button.get_attribute("data-category") == category_to_test
                    )
                    pie_legend.click()
                    WebDriverWait(driver, args.timeout).until(
                        lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-filter"]').get_attribute("value") == "all"
                        and current_sankey_node_names(current) == full_cashflow_node_names
                    )
                    sankey_interaction_checks["category_node_and_pie_share_filter"] = pie_legend.get_attribute("aria-pressed") == "false"

                sankey_links = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-link"]')
                sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                aggregated_cashflow_link = next(
                    (link for link in sankey_links if int(link.get_attribute("data-sankey-transaction-count") or 0) > 1),
                    None,
                )
                detail_flow_links = [
                    link for link in sankey_links
                    if link.get_attribute("data-sankey-target-type") != "category"
                ]
                selected_flow_link = detail_flow_links[0] if detail_flow_links else sankey_links[0]
                selected_flow_transaction_count = int(selected_flow_link.get_attribute("data-sankey-transaction-count") or 0)
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", selected_flow_link)
                details_panel = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                detail_transaction_count = len(details_panel.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]'))
                sankey_interaction_checks["flow_click_shows_transactions"] = (
                    details_panel.get_attribute("data-sankey-selection-type") == "link"
                    and "→" in details_panel.text
                    and detail_transaction_count > 0
                )
                sankey_interaction_checks["repeated_account_category_transactions_share_link"] = (
                    aggregated_cashflow_link is not None
                    and int(aggregated_cashflow_link.get_attribute("data-sankey-transaction-count") or 0) > 1
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", details_panel)
                driver.save_screenshot(str(sankey_details_screenshot))

                sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                detail_nodes = [node for node in sankey_nodes if node.get_attribute("data-sankey-node-type") != "category"]
                driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", detail_nodes[0])
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", detail_nodes[0])
                node_panel = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                sankey_interaction_checks["node_click_shows_transactions"] = (
                    node_panel.get_attribute("data-sankey-selection-type") == "node"
                    and bool(node_panel.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]'))
                )

                sankey_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                detail_nodes = [node for node in sankey_nodes if node.get_attribute("data-sankey-node-type") != "category"]
                detail_nodes[0].send_keys(Keys.ENTER)
                keyboard_panel = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                sankey_interaction_checks["node_keyboard_selection"] = (
                    keyboard_panel.get_attribute("data-sankey-selection-type") == "node"
                    and bool(keyboard_panel.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]'))
                )

                driver.execute_script("arguments[0].click();", driver.find_element(By.CSS_SELECTOR, '[data-testid="sankey-cashflow-tab"]'))
                WebDriverWait(driver, args.timeout).until(
                    lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="sankey-cashflow-tab"]').get_attribute("aria-selected") == "true"
                )
                sankey_interaction_checks["cashflow_excludes_internal_transfers"] = not any(
                    node.get_attribute("data-sankey-node-name") in {"Checking", "Savings"}
                    for node in driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                )

                driver.execute_script("arguments[0].click();", driver.find_element(By.CSS_SELECTOR, '[data-testid="sankey-transfers-tab"]'))
                WebDriverWait(driver, args.timeout).until(
                    lambda current: current.find_element(By.CSS_SELECTOR, '[data-testid="sankey-transfers-tab"]').get_attribute("aria-selected") == "true"
                    and len(current.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-link"]')) >= 4
                )
                WebDriverWait(driver, args.timeout).until(
                    lambda current: any(
                        node.get_attribute("data-sankey-node-name") == "External / unmatched"
                        for node in current.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                    )
                )
                if args.device in {"iphone", "pixel"}:
                    transfer_layout_metrics = sankey_mobile_layout_metrics(driver)
                    sankey_mobile_measurements["transfers"] = transfer_layout_metrics
                    sankey_layout_checks["transfers_has_readable_mobile_width"] = (
                        transfer_layout_metrics["svgWidth"] >= 640
                        and transfer_layout_metrics["scrollWidth"] > transfer_layout_metrics["containerWidth"] + 1
                    )
                    sankey_layout_checks["transfers_right_labels_reachable_by_swipe"] = transfer_layout_metrics[
                        "rightmostLabelReachable"
                    ]
                    sankey_layout_checks["transfers_labels_do_not_overlap_or_clip"] = (
                        transfer_layout_metrics["labelOverlaps"] == 0
                        and transfer_layout_metrics["labelNodeOverlaps"] == 0
                        and transfer_layout_metrics["clippedLabels"] == 0
                    )
                    sankey_layout_checks["transfers_container_matches_svg_height"] = abs(
                        transfer_layout_metrics["containerHeight"] - transfer_layout_metrics["renderedSvgHeight"]
                    ) <= 1
                transfer_links = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-link"]')
                paired_counts = [int(link.get_attribute("data-paired-transfer-count") or 0) for link in transfer_links]
                transfer_nodes = driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                node_sides = {
                    (node.get_attribute("data-sankey-node-name"), node.get_attribute("data-sankey-node-side"))
                    for node in transfer_nodes
                }
                sankey_interaction_checks["linked_transfers_match_across_account_types"] = sum(paired_counts) == 3 and (
                    ("Test Credit Union · Checking", "source") in node_sides
                    and ("Test Bank · Checking", "destination") in node_sides
                )
                sankey_interaction_checks["reverse_transfers_render_without_cycles"] = (
                    ("Savings", "source") in node_sides and ("Savings", "destination") in node_sides
                    and all(link.is_displayed() for link in transfer_links)
                )
                sankey_interaction_checks["unmatched_transfer_entries_remain_visible"] = any(
                    node.get_attribute("data-sankey-node-name") == "External / unmatched"
                    for node in transfer_nodes
                )
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", driver.find_element(By.CSS_SELECTOR, '[data-testid="sankey-transfers-tab"]'))
                driver.save_screenshot(str(transfer_screenshot))

                paired_link = next(link for link in transfer_links if int(link.get_attribute("data-paired-transfer-count") or 0) > 0)
                driver.execute_script("arguments[0].dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));", paired_link)
                transfer_details = WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="sankey-details"]'))
                )
                sankey_interaction_checks["paired_transfer_details_show_both_entries"] = (
                    "counted once" in transfer_details.text
                    and len(transfer_details.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-detail-transaction"]')) == 2
                )

            editable_row = next(
                row for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
                if "Grocery Store" in row.text
            )
            editable_transaction_id = editable_row.get_attribute("data-transaction-id")
            editable_category = Select(editable_row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]'))
            editable_category.select_by_value("Shopping")
            category_prompt = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="category-rule-prompt"]'))
            )
            category_rule_prompt_screenshot = screenshot.with_name(screenshot.stem + "-category-rule-prompt" + screenshot.suffix)
            driver.save_screenshot(str(category_rule_prompt_screenshot))
            transaction_filter_checks["manual_category_change_prompts_for_future_rule"] = (
                "Grocery Store" in category_prompt.text
                and "Shopping" in category_prompt.text
                and bool(category_prompt.find_elements(By.CSS_SELECTOR, '[data-testid="category-rule-apply"]'))
            )
            category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-one-off"]').click()
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'{transaction_row_selector}[data-transaction-id="{editable_transaction_id}"]')
                    .find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]').get_attribute("value") == "Shopping"
                and not current.find_elements(By.CSS_SELECTOR, '[data-testid="category-rule-prompt"]')
            )
            transaction_filter_checks["manual_category_edit_works"] = True
            with urllib.request.urlopen(base_url + "api/category-rules", timeout=10) as response:
                one_off_rules = json.loads(response.read().decode("utf-8")).get("rules_json") or ""
            transaction_filter_checks["one_off_choice_does_not_create_rule"] = not one_off_rules

            coffee_row = next(
                row for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
                if "Coffee Shop" in row.text
            )
            coffee_transaction_id = coffee_row.get_attribute("data-transaction-id")
            Select(coffee_row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]')).select_by_value("Shopping")
            category_prompt = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="category-rule-prompt"]'))
            )
            rule_source_select = Select(category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-match-source"]'))
            rule_source_select.select_by_value("description")
            WebDriverWait(driver, args.timeout).until(
                lambda current: bool(current.find_elements(By.CSS_SELECTOR, '[data-testid="category-rule-no-existing-matches"]'))
                and not current.find_elements(By.CSS_SELECTOR, '[data-testid="category-rule-apply-existing"]')
            )
            transaction_filter_checks["description_source_recalculates_existing_matches"] = (
                category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-match-source"]').get_attribute("value") == "description"
            )
            rule_source_select = Select(category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-match-source"]'))
            rule_source_select.select_by_value("both")
            category_prompt = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="category-rule-prompt"]'))
            )
            existing_match_button = category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-apply-existing"]')
            transaction_filter_checks["category_rule_match_source_is_selectable"] = (
                category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-match-source"]').get_attribute("value") == "both"
                and "either" in category_prompt.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-match-source"]').text.lower()
            )
            transaction_filter_checks["retroactive_prompt_shows_count_and_warning"] = (
                "1 other matching transaction" in category_prompt.text
                and "manually" in category_prompt.text
                and "update 1 past match" in existing_match_button.text.lower()
            )
            category_rule_existing_prompt_screenshot = screenshot.with_name(
                screenshot.stem + "-category-rule-existing-prompt" + screenshot.suffix
            )
            driver.save_screenshot(str(category_rule_existing_prompt_screenshot))
            existing_match_button.click()
            WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'{transaction_row_selector}[data-transaction-id="{coffee_transaction_id}"]')
                    .find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]').get_attribute("value") == "Shopping"
                and not current.find_elements(By.CSS_SELECTOR, '[data-testid="category-rule-prompt"]')
            )
            coffee_rows = [
                row for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
                if "Coffee Shop" in row.text
            ]
            transaction_filter_checks["future_rule_choice_saves_selected_patterns"] = bool(coffee_rows)
            transaction_filter_checks["retroactive_choice_updates_all_matching_entries"] = (
                len(coffee_rows) >= 2
                and all(
                    row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-category-select"]').get_attribute("value") == "Shopping"
                    for row in coffee_rows
                )
            )
            with urllib.request.urlopen(base_url + "api/category-rules", timeout=10) as response:
                saved_rules = json.loads(json.loads(response.read().decode("utf-8")).get("rules_json") or "[]")
            transaction_filter_checks["future_rule_choice_preserves_existing_defaults"] = (
                saved_rules
                and saved_rules[0].get("category") == "Shopping"
                and "Coffee Shop" in saved_rules[0].get("patterns", [])
                and "Latte" in saved_rules[0].get("patterns", [])
                and any(rule.get("category") == "Groceries" and "grocery" in rule.get("patterns", []) for rule in saved_rules)
                and any(rule.get("category") == "Income" and "payroll" in rule.get("patterns", []) for rule in saved_rules)
            )

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.CSS_SELECTOR, '[data-testid="settings-nav-general"]').click()
            WebDriverWait(driver, args.timeout).until(lambda current: "General settings" in current.find_element(By.TAG_NAME, "body").text)
            settings_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            settings_navigation_checks = {
                "general_page_has_general_panels_only": (
                    bool(driver.find_elements(By.CSS_SELECTOR, '[data-testid="account-type-settings"]'))
                    and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="category-management-settings"]')
                    and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="ai-settings"]')
                ),
                "categories_page_has_category_panel_only": False,
                "ai_page_has_ai_panel_only": False,
                "settings_menu_has_three_entries": False,
            }

            custom_category_checks = {
                "pets_category_available": False,
                "childcare_category_available": False,
                "custom_category_can_be_added": False,
            }
            account_type_checks = {
                "account_type_settings_visible": False,
                "venture_auto_classified": False,
                "liability_type_can_be_saved": False,
                "asset_type_can_be_saved": False,
                "nickname_saves_and_updates_account_name": False,
                "liability_balance_shows_as_debt": False,
                "net_worth_subtracts_debt": False,
            }
            account_type_section = driver.find_element(By.CSS_SELECTOR, '[data-testid="account-type-settings"]')
            account_type_checks["account_type_settings_visible"] = "Account types and net worth" in account_type_section.text
            venture_type_row = next(
                row for row in account_type_section.find_elements(By.CSS_SELECTOR, '[data-testid="account-type-row"]')
                if "Venture X" in row.text
            )
            venture_account_id = venture_type_row.get_attribute("data-account-id")
            venture_type_select = Select(venture_type_row.find_element(By.TAG_NAME, "select"))
            account_type_checks["venture_auto_classified"] = (
                "Auto-classified as Credit card debt" in venture_type_row.text
                and venture_type_select.first_selected_option.get_attribute("value") == "credit_card"
            )
            venture_type_select.select_by_value("other_liability")
            WebDriverWait(driver, args.timeout).until(
                lambda _current: venture_type_row.find_element(By.TAG_NAME, "select").get_attribute("value") == "other_liability"
            )
            venture_type_select.select_by_value("credit_card")
            WebDriverWait(driver, args.timeout).until(
                lambda _current: venture_type_row.find_element(By.TAG_NAME, "select").get_attribute("value") == "credit_card"
            )
            account_type_checks["liability_type_can_be_saved"] = True

            checking_type_row = next(
                row for row in account_type_section.find_elements(By.CSS_SELECTOR, '[data-testid="account-type-row"]')
                if "Checking" in row.text and "Navy Federal" in row.text
            )
            checking_type_select = Select(checking_type_row.find_element(By.TAG_NAME, "select"))
            checking_type_select.select_by_value("other_asset")
            WebDriverWait(driver, args.timeout).until(
                lambda _current: checking_type_row.find_element(By.TAG_NAME, "select").get_attribute("value") == "other_asset"
            )
            checking_type_select.select_by_value("bank_account")
            WebDriverWait(driver, args.timeout).until(
                lambda _current: checking_type_row.find_element(By.TAG_NAME, "select").get_attribute("value") == "bank_account"
            )
            account_type_checks["asset_type_can_be_saved"] = True

            venture_type_row = driver.find_element(By.CSS_SELECTOR, f'[data-testid="account-type-row"][data-account-id="{venture_account_id}"]')
            nickname_input = venture_type_row.find_element(By.CSS_SELECTOR, f'[data-testid="account-nickname-input-{venture_account_id}"]')
            driver.execute_script("const input=arguments[0], value=arguments[1]; const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; setter.call(input, value); input.dispatchEvent(new Event('change', {bubbles:true}));", nickname_input, "Travel Card")
            WebDriverWait(driver, args.timeout).until(
                lambda current: "Travel Card" in current.find_element(By.CSS_SELECTOR, f'[data-testid="account-type-row"][data-account-id="{venture_account_id}"]').text
            )
            account_type_checks["nickname_saves_and_updates_account_name"] = True

            driver.execute_script("window.scrollTo(0, 0)")
            driver.save_screenshot(str(screenshot.with_name(screenshot.stem + "-settings-general" + screenshot.suffix)))
            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            settings_navigation_checks["settings_menu_has_three_entries"] = all(
                driver.find_elements(By.CSS_SELECTOR, f'[data-testid="{test_id}"]')
                for test_id in ("settings-nav-general", "settings-nav-categories", "settings-nav-ai")
            )
            driver.save_screenshot(str(screenshot.with_name(screenshot.stem + "-settings-menu" + screenshot.suffix)))
            driver.find_element(By.CSS_SELECTOR, '[data-testid="settings-nav-categories"]').click()
            category_management = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="category-management-settings"]'))
            )
            settings_navigation_checks["categories_page_has_category_panel_only"] = (
                "Category rules" in category_management.text
                and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="account-type-settings"]')
                and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="ai-settings"]')
            )
            settings_overflow = settings_overflow or driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

            category_select = driver.find_element(By.CSS_SELECTOR, '[data-testid="category-rule-select"]')
            options = [option.get_attribute("value") for option in category_select.find_elements(By.TAG_NAME, "option")]
            custom_category_checks["pets_category_available"] = "Pets" in options
            custom_category_checks["childcare_category_available"] = "Childcare" in options
            custom_category_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="custom-category-input"]')
            custom_category_input.send_keys("Selenium Custom")
            driver.execute_script("arguments[0].click();", driver.find_element(By.CSS_SELECTOR, '[data-testid="add-custom-category"]'))
            category_selects = driver.find_elements(By.CSS_SELECTOR, '[data-testid="category-rule-select"]')
            custom_category_checks["custom_category_can_be_added"] = any(
                option.get_attribute("value") == "Selenium Custom"
                for select in category_selects
                for option in select.find_elements(By.TAG_NAME, "option")
            )
            driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//button[normalize-space()='Use JSON']"))
            custom_category_in_json = driver.find_element(By.CSS_SELECTOR, '[data-testid="category-rules-json"]').get_property("value")
            custom_category_checks["custom_category_can_be_added"] = (
                custom_category_checks["custom_category_can_be_added"]
                and '"category": "Selenium Custom"' in custom_category_in_json
                and '"patterns": []' in custom_category_in_json
            )
            driver.execute_script("arguments[0].click();", driver.find_element(By.XPATH, "//button[normalize-space()='Use builder']"))
            driver.execute_script("window.scrollTo(0, 0)")
            driver.save_screenshot(str(screenshot.with_name(screenshot.stem + "-settings-categories" + screenshot.suffix)))

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.CSS_SELECTOR, '[data-testid="settings-nav-ai"]').click()
            ai_settings_panel = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="ai-settings"]'))
            )
            settings_navigation_checks["ai_page_has_ai_panel_only"] = (
                "AI assistance" in ai_settings_panel.text
                and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="category-management-settings"]')
                and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="account-type-settings"]')
            )
            settings_overflow = settings_overflow or driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")
            driver.execute_script("window.scrollTo(0, 0)")
            driver.save_screenshot(str(screenshot.with_name(screenshot.stem + "-settings-ai" + screenshot.suffix)))

            goal_checks = {
                "goals_page_opens": False,
                "one_goal_creation_entry_point": False,
                "add_button_hides_during_creation": False,
                "debt_goal_fields_visible": False,
                "retirement_goal_fields_visible": False,
                "savings_goal_can_be_created": False,
                "goal_can_be_edited": False,
                "goal_progress_appears_on_dashboard": False,
                "goal_can_be_deleted": False,
            }
            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.CSS_SELECTOR, '[data-testid="goals-nav"]').click()
            goals_page = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="goals-page"]'))
            )
            goal_checks["goals_page_opens"] = "Financial goals" in goals_page.text
            goal_checks["one_goal_creation_entry_point"] = (
                len(driver.find_elements(By.CSS_SELECTOR, '[data-testid="goal-add-button"]')) == 1
                and not driver.find_elements(By.XPATH, "//button[normalize-space()='Create your first goal']")
                and bool(driver.find_elements(By.CSS_SELECTOR, '[data-testid="ai-goal-assistant"]'))
            )
            driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-add-button"]').click()
            goal_checks["add_button_hides_during_creation"] = not driver.find_elements(
                By.CSS_SELECTOR, '[data-testid="goal-add-button"]'
            )
            goal_form_screenshot = screenshot.with_name(screenshot.stem + "-goal-form" + screenshot.suffix)
            driver.execute_script("window.scrollTo(0, 0)")
            driver.save_screenshot(str(goal_form_screenshot))
            goal_type_select = Select(driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-type"]'))
            goal_type_select.select_by_value("retirement")
            WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="goal-return"]'))
            )
            goal_checks["retirement_goal_fields_visible"] = (
                bool(driver.find_elements(By.CSS_SELECTOR, '[data-testid="goal-retirement-year"]'))
                and bool(driver.find_elements(By.CSS_SELECTOR, '[data-testid="goal-inflation"]'))
            )
            retirement_goal_form_screenshot = screenshot.with_name(screenshot.stem + "-goal-retirement-form" + screenshot.suffix)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-return"]'))
            driver.save_screenshot(str(retirement_goal_form_screenshot))
            goal_type_select.select_by_value("debt_payoff")
            WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="goal-apr"]'))
            )
            goal_checks["debt_goal_fields_visible"] = bool(driver.find_elements(By.CSS_SELECTOR, '[data-testid="goal-target-date"]'))
            goal_type_select.select_by_value("savings")
            goal_name_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-name"]')
            goal_name_input.clear()
            goal_name_input.send_keys("Selenium Savings Goal")
            driver.find_element(By.CSS_SELECTOR, '[data-goal-account-id="3"]').click()
            driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-target-amount"]').send_keys("5000")
            driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-save"]').click()
            saved_goal = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="goal-card"]'))
            )
            goal_id = saved_goal.get_attribute("data-goal-id")
            goal_checks["savings_goal_can_be_created"] = (
                "Selenium Savings Goal" in saved_goal.text
                and "3,400.50" in saved_goal.text
                and "68%" in saved_goal.text
            )
            goals_screenshot = screenshot.with_name(screenshot.stem + "-goals" + screenshot.suffix)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", saved_goal)
            driver.save_screenshot(str(goals_screenshot))

            saved_goal.find_element(By.XPATH, ".//button[starts-with(@aria-label, 'Edit ')]").click()
            goal_name_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-name"]')
            goal_name_input.clear()
            goal_name_input.send_keys("Selenium Reserve")
            goal_amount_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-target-amount"]')
            goal_amount_input.clear()
            goal_amount_input.send_keys("6000")
            driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-save"]').click()
            edited_goal = WebDriverWait(driver, args.timeout).until(
                lambda current: current.find_element(By.CSS_SELECTOR, f'[data-testid="goal-card"][data-goal-id="{goal_id}"]')
                if "Selenium Reserve" in current.find_element(By.CSS_SELECTOR, f'[data-testid="goal-card"][data-goal-id="{goal_id}"]').text
                else False
            )
            goal_checks["goal_can_be_edited"] = "6,000.00" in edited_goal.text

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.XPATH, "//button[.//span[text()='Dashboard']]").click()
            dashboard_goal = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="dashboard-goal-card"]'))
            )
            goal_checks["goal_progress_appears_on_dashboard"] = "Selenium Reserve" in dashboard_goal.text
            dashboard_goals_screenshot = screenshot.with_name(screenshot.stem + "-dashboard-goals" + screenshot.suffix)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", dashboard_goal)
            driver.save_screenshot(str(dashboard_goals_screenshot))

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.CSS_SELECTOR, '[data-testid="goals-nav"]').click()
            edited_goal = driver.find_element(By.CSS_SELECTOR, f'[data-testid="goal-card"][data-goal-id="{goal_id}"]')
            edited_goal.find_element(By.XPATH, ".//button[starts-with(@aria-label, 'Delete ')]").click()
            driver.switch_to.alert.accept()
            WebDriverWait(driver, args.timeout).until(
                lambda current: not current.find_elements(By.CSS_SELECTOR, '[data-testid="goal-card"]')
            )
            goal_checks["goal_can_be_deleted"] = bool(driver.find_elements(By.CSS_SELECTOR, '[data-testid="goals-empty"]'))
            goals_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

            ai_chat_checks = {
                "assistant_is_embedded_in_goals": False,
                "sanitized_preview_removes_known_identifiers": False,
                "sanitized_chat_can_be_sent": False,
                "goal_draft_opens_review_form": False,
                "draft_requires_user_save": False,
            }
            goals_page = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="goals-page"]'))
            )
            ai_chat_checks["assistant_is_embedded_in_goals"] = (
                "AI goal planner" in goals_page.text
                and bool(goals_page.find_elements(By.CSS_SELECTOR, '[data-testid="ai-goal-assistant"]'))
                and not driver.find_elements(By.CSS_SELECTOR, '[data-testid="ai-chat-nav"]')
            )
            ai_chat_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="ai-chat-message"]')
            ai_chat_input.send_keys("Help me plan with Travel Card 123456789 for Groceries and about $500 monthly.")
            driver.find_element(By.CSS_SELECTOR, '[data-testid="ai-chat-preview-button"]').click()
            sanitized_chat_element = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="chat-sanitized-message"]'))
            )
            sanitized_chat_message = sanitized_chat_element.text
            ai_chat_checks["sanitized_preview_removes_known_identifiers"] = (
                "Travel Card" not in sanitized_chat_message
                and "123456789" not in sanitized_chat_message
                and "Groceries" in sanitized_chat_message
                and "$500" in sanitized_chat_message
            )
            ai_chat_screenshot = screenshot.with_name(screenshot.stem + "-ai-chat-preview" + screenshot.suffix)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", sanitized_chat_element)
            driver.save_screenshot(str(ai_chat_screenshot))
            driver.find_element(By.CSS_SELECTOR, '[data-testid="ai-chat-send-button"]').click()
            assistant_message = WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="ai-chat-assistant-message"]'))
            )
            ai_chat_checks["sanitized_chat_can_be_sent"] = (
                "savings goal draft" in assistant_message.text.lower()
                and "Travel Card" not in driver.find_element(By.CSS_SELECTOR, '[data-testid="ai-chat-user-message"]').text
            )
            draft_button = assistant_message.find_element(By.CSS_SELECTOR, '[data-testid="ai-chat-review-goal-draft"]')
            draft_button.click()
            WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="goals-page"] [data-testid="goal-form"]'))
            )
            ai_draft_name = driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-name"]').get_attribute("value")
            ai_draft_amount = driver.find_element(By.CSS_SELECTOR, '[data-testid="goal-target-amount"]').get_attribute("value")
            ai_chat_checks["goal_draft_opens_review_form"] = ai_draft_name == "Emergency fund" and ai_draft_amount == "10000"
            ai_chat_checks["draft_requires_user_save"] = not driver.find_elements(By.CSS_SELECTOR, '[data-testid="goal-card"]')
            ai_chat_overflow = driver.execute_script("return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1")

            driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
            driver.find_element(By.XPATH, "//button[.//span[text()='Dashboard']]").click()
            WebDriverWait(driver, args.timeout).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="net-worth-summary"]'))
            )
            venture_card = driver.find_element(By.CSS_SELECTOR, f'[data-testid="account-filter-card"][data-account-id="{venture_account_id}"]')
            venture_balance = venture_card.find_element(By.CSS_SELECTOR, '[data-testid="account-balance"]')
            account_type_checks["liability_balance_shows_as_debt"] = (
                "-$1,250.00" in venture_balance.text
                and "text-rose-700" in venture_balance.get_attribute("class")
            )
            grocery_row = next(
                row for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector)
                if "Grocery Store" in row.text
            )
            account_type_checks["nickname_saves_and_updates_account_name"] = (
                account_type_checks["nickname_saves_and_updates_account_name"]
                and "Travel Card" in grocery_row.find_element(By.CSS_SELECTOR, '[data-testid="transaction-account"]').text
            )
            net_worth_text = driver.find_element(By.CSS_SELECTOR, '[data-testid="net-worth-summary"]').text
            account_type_checks["net_worth_subtracts_debt"] = (
                "USD" in net_worth_text
                and "$1,250.00" in net_worth_text
                and "$173,600.50" in net_worth_text
                and "$172,350.50" in net_worth_text
            )

            assets_screenshot = None
            if test_mode == "real-fastapi-isolated-db":
                manual_asset_checks = {
                    "sidebar_assets_menu_opens_manager": False,
                    "assets_view_opens_at_top": False,
                    "empty_state_is_clear": False,
                    "asset_can_be_added": False,
                    "asset_can_be_edited": False,
                    "asset_value_updates_net_worth": False,
                    "asset_can_be_removed": False,
                    "net_worth_reverts_after_removal": False,
                    "manual_assets_do_not_clutter_dashboard": False,
                    "manual_assets_excluded_from_transactions_and_sankey": False,
                    "assets_page_no_horizontal_overflow": True,
                }
                baseline_asset_card = driver.find_element(
                    By.CSS_SELECTOR,
                    '[data-testid="net-worth-currency"][data-currency="USD"]',
                )
                baseline_asset_total = float(baseline_asset_card.get_attribute("data-assets-total"))

                driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
                assets_menu_button = wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Assets']]"))
                )
                assets_menu_button.click()
                assets_view = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="manual-assets-view"]')))
                manual_asset_checks["sidebar_assets_menu_opens_manager"] = "Manual assets" in assets_view.text
                manual_asset_checks["assets_view_opens_at_top"] = driver.execute_script("return window.scrollY === 0")
                manual_asset_checks["empty_state_is_clear"] = bool(
                    driver.find_elements(By.CSS_SELECTOR, '[data-testid="manual-assets-empty"]')
                )
                assets_screenshot = screenshot.with_name(screenshot.stem + "-assets" + screenshot.suffix)
                driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-name"]').send_keys("Selenium Home")
                driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-value"]').send_keys("300000")
                driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-currency"]').clear()
                driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-currency"]').send_keys("USD")
                driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-save"]').click()
                WebDriverWait(driver, args.timeout).until(
                    lambda current: len(current.find_elements(By.CSS_SELECTOR, '[data-testid="manual-asset-row"]')) == 1
                )
                manual_asset_checks["asset_can_be_added"] = (
                    "Selenium Home" in driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-row"]').text
                    and "$300,000.00" in driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-row"]').text
                )
                driver.save_screenshot(str(assets_screenshot))
                manual_asset_checks["assets_page_no_horizontal_overflow"] = not driver.execute_script(
                    "return document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
                )

                driver.find_element(By.CSS_SELECTOR, '[aria-label="Edit Selenium Home"]').click()
                value_input = driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-value"]')
                value_input.clear()
                value_input.send_keys("325000")
                driver.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-save"]').click()
                WebDriverWait(driver, args.timeout).until(
                    lambda current: "$325,000.00" in current.find_element(By.CSS_SELECTOR, '[data-testid="manual-asset-row"]').text
                )
                manual_asset_checks["asset_can_be_edited"] = True

                driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
                driver.find_element(By.XPATH, "//button[.//span[text()='Dashboard']]").click()
                WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="net-worth-summary"]'))
                )
                updated_asset_card = driver.find_element(
                    By.CSS_SELECTOR,
                    '[data-testid="net-worth-currency"][data-currency="USD"]',
                )
                updated_asset_total = float(updated_asset_card.get_attribute("data-assets-total"))
                manual_asset_checks["asset_value_updates_net_worth"] = abs(
                    updated_asset_total - baseline_asset_total - 325000
                ) < 0.02
                manual_asset_checks["manual_assets_do_not_clutter_dashboard"] = not driver.find_elements(
                    By.CSS_SELECTOR,
                    '[data-testid="manual-asset-row"]',
                )
                manual_asset_checks["manual_assets_excluded_from_transactions_and_sankey"] = (
                    all("Selenium Home" not in row.text for row in driver.find_elements(By.CSS_SELECTOR, transaction_row_selector))
                    and all(
                        "Selenium Home" not in (node.get_attribute("data-sankey-node-name") or "")
                        for node in driver.find_elements(By.CSS_SELECTOR, '[data-testid="sankey-node"]')
                    )
                )

                driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
                driver.find_element(By.XPATH, "//button[.//span[text()='Assets']]").click()
                driver.find_element(By.CSS_SELECTOR, '[aria-label="Remove Selenium Home"]').click()
                WebDriverWait(driver, args.timeout).until(EC.alert_is_present()).accept()
                WebDriverWait(driver, args.timeout).until(
                    lambda current: bool(current.find_elements(By.CSS_SELECTOR, '[data-testid="manual-assets-empty"]'))
                )
                manual_asset_checks["asset_can_be_removed"] = True

                driver.find_element(By.CSS_SELECTOR, 'button[aria-label="Open menu"]').click()
                driver.find_element(By.XPATH, "//button[.//span[text()='Dashboard']]").click()
                WebDriverWait(driver, args.timeout).until(
                    EC.visibility_of_element_located((By.CSS_SELECTOR, '[data-testid="net-worth-summary"]'))
                )
                final_asset_card = driver.find_element(
                    By.CSS_SELECTOR,
                    '[data-testid="net-worth-currency"][data-currency="USD"]',
                )
                final_asset_total = float(final_asset_card.get_attribute("data-assets-total"))
                manual_asset_checks["net_worth_reverts_after_removal"] = abs(
                    final_asset_total - baseline_asset_total
                ) < 0.02
            else:
                manual_asset_checks = {"live_asset_mutation_skipped": True}

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
                "index_html_no_store_cache": index_html_no_store_cache,
                "boot_screen_removed": not boot_screen_visible,
                "dashboard_screenshot_content": dashboard_ratio >= 0.003,
                "header_scroll_behavior": header_scroll_checks,
                "recurring_screenshot_content": recurring_ratio >= 0.003,
                "no_severe_browser_errors": not severe,
                "no_vue_warnings": not vue_warnings,
                "recurring_quick_filters": filter_checks,
                "sankey_interactions": sankey_interaction_checks,
                "sankey_crossing_order": sankey_crossing_check,
                "sankey_link_aggregation": sankey_link_aggregation_check,
                "sankey_crossing_metrics": sankey_crossing_metrics,
                "sankey_mobile_layout": sankey_layout_checks,
                "sankey_mobile_measurements": sankey_mobile_measurements,
                "transaction_grouping": transaction_group_checks,
                "transaction_search_and_category_filter": transaction_filter_checks,
                "spending_pie": spending_pie_checks,
                "account_types_and_net_worth": account_type_checks,
                "settings_navigation": settings_navigation_checks,
                "financial_goals": goal_checks,
                "ai_chat_privacy": ai_chat_checks,
                "manual_assets": manual_asset_checks,
                "transaction_description_fits_card": transaction_description_fits_card,
                "ha_theme_auto_detection": theme_checks,
                "custom_categories": custom_category_checks,
                "no_external_runtime_scripts": not external_script_urls,
                "dashboard_no_horizontal_overflow": not dashboard_overflow,
                "recurring_no_horizontal_overflow": not recurring_overflow,
                "settings_no_horizontal_overflow": not settings_overflow,
                "goals_no_horizontal_overflow": not goals_overflow,
                "ai_chat_no_horizontal_overflow": not ai_chat_overflow,
                "show_more_transactions_works": show_more_works,
            }
            ok = (
                all(checks["dashboard_sections"].values())
                and all(checks["recurring_sections"].values())
                and all(status == 200 for status in api_statuses.values())
                and index_html_no_store_cache
                and checks["boot_screen_removed"]
                and checks["dashboard_screenshot_content"]
                and all(header_scroll_checks.values())
                and checks["recurring_screenshot_content"]
                and checks["no_severe_browser_errors"]
                and checks["no_vue_warnings"]
                and all(filter_checks.values())
                and all(sankey_interaction_checks.values())
                and checks["sankey_crossing_order"]
                and checks["sankey_link_aggregation"]
                and all(sankey_layout_checks.values())
                and all(transaction_group_checks.values())
                and all(transaction_filter_checks.values())
                and all(spending_pie_checks.values())
                and all(account_type_checks.values())
                and all(settings_navigation_checks.values())
                and all(goal_checks.values())
                and all(ai_chat_checks.values())
                and all(manual_asset_checks.values())
                and transaction_description_fits_card
                and all(theme_checks.values())
                and all(custom_category_checks.values())
                and checks["no_external_runtime_scripts"]
                and checks["dashboard_no_horizontal_overflow"]
                and checks["recurring_no_horizontal_overflow"]
                and checks["settings_no_horizontal_overflow"]
                and checks["goals_no_horizontal_overflow"]
                and checks["ai_chat_no_horizontal_overflow"]
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
                "transfer_screenshot": str(transfer_screenshot) if args.device in {"iphone", "pixel"} else None,
                "transaction_screenshot": str(transaction_screenshot) if transaction_screenshot else None,
                "category_rule_prompt_screenshot": str(category_rule_prompt_screenshot) if category_rule_prompt_screenshot else None,
                "category_rule_existing_prompt_screenshot": str(category_rule_existing_prompt_screenshot) if category_rule_existing_prompt_screenshot else None,
                "general_settings_screenshot": str(screenshot.with_name(screenshot.stem + "-settings-general" + screenshot.suffix)),
                "category_settings_screenshot": str(screenshot.with_name(screenshot.stem + "-settings-categories" + screenshot.suffix)),
                "ai_settings_screenshot": str(screenshot.with_name(screenshot.stem + "-settings-ai" + screenshot.suffix)),
                "goals_screenshot": str(goals_screenshot),
                "dashboard_goals_screenshot": str(dashboard_goals_screenshot),
                "goal_form_screenshot": str(goal_form_screenshot),
                "retirement_goal_form_screenshot": str(retirement_goal_form_screenshot),
                "ai_chat_preview_screenshot": str(ai_chat_screenshot),
                "sankey_filtered_screenshot": str(sankey_filtered_screenshot) if sankey_filtered_screenshot else None,
                "sankey_restored_screenshot": str(sankey_restored_screenshot) if sankey_restored_screenshot else None,
                "account_filter_screenshot": str(account_filter_screenshot) if account_filter_screenshot else None,
                "spending_pie_screenshot": str(spending_pie_screenshot) if spending_pie_screenshot else None,
                "dark_mode_screenshot": str(dark_mode_screenshot) if dark_mode_screenshot else None,
                "assets_screenshot": str(assets_screenshot) if assets_screenshot else None,
                "checks": checks,
                "external_script_urls": external_script_urls,
                "severe_browser_errors": severe,
                "vue_warnings": vue_warnings,
            }, indent=2))
            if not ok:
                raise SystemExit(1)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
            driver = None
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        if browser_profile_dir is not None:
            browser_profile_dir.cleanup()
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=8)
        if test_data_dir is not None:
            test_data_dir.cleanup()


if __name__ == "__main__":
    main()
