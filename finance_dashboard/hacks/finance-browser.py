#!/usr/bin/env python3
"""Run a bounded selector-based Selenium action list against the authenticated Finance panel."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

PANEL_PATH = "/10d6f312_custom_finance"
MAX_ACTIONS = 20
MUTATING_ACTIONS = {"click", "fill", "select", "keypress"}
ALLOWED_ACTIONS = MUTATING_ACTIONS | {"wait", "count", "screenshot"}
KEYS = {
    "ENTER": Keys.ENTER,
    "ESCAPE": Keys.ESCAPE,
    "TAB": Keys.TAB,
    "SPACE": Keys.SPACE,
    "ARROW_UP": Keys.ARROW_UP,
    "ARROW_DOWN": Keys.ARROW_DOWN,
    "ARROW_LEFT": Keys.ARROW_LEFT,
    "ARROW_RIGHT": Keys.ARROW_RIGHT,
    "HOME": Keys.HOME,
    "END": Keys.END,
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actions-file", type=Path, help="JSON array of selector actions; omit for a screenshot-only check.")
    parser.add_argument("--apply", action="store_true", help="Allow clicks, typing, selects, and keypresses that may change live data.")
    parser.add_argument("--width", type=int, default=390)
    parser.add_argument("--height", type=int, default=844)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--screenshot-name", default="finance-dashboard-agent.png")
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


def load_actions(path: Path | None) -> list[dict]:
    if path is None:
        return []
    actions = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(actions, list) or len(actions) > MAX_ACTIONS:
        raise ValueError(f"Actions must be a JSON array with at most {MAX_ACTIONS} items.")
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in ALLOWED_ACTIONS:
            raise ValueError("Each action must be an object with a supported action type.")
        if action["type"] != "screenshot":
            selector = action.get("selector")
            if not isinstance(selector, str) or not selector.strip() or len(selector) > 500:
                raise ValueError("Each DOM action needs a non-empty selector of at most 500 characters.")
        if action["type"] in {"fill", "select"} and not isinstance(action.get("value"), str):
            raise ValueError(f"{action['type']} actions require a string value.")
        if action["type"] == "keypress" and action.get("key") not in KEYS:
            raise ValueError("keypress supports only the named keys: " + ", ".join(KEYS))
    return actions


def locate_finance_frame(driver) -> bool:
    """Focus the app frame if HA rendered Ingress inside an iframe."""
    driver.switch_to.default_content()
    frames = driver.execute_script("""
        const frames = [];
        const roots = [document];
        const seen = new Set();
        while (roots.length) {
          const root = roots.pop();
          if (!root || seen.has(root)) continue;
          seen.add(root);
          for (const el of root.querySelectorAll('*')) {
            if (el.tagName === 'IFRAME') frames.push(el);
            if (el.shadowRoot) roots.push(el.shadowRoot);
          }
        }
        return frames;
    """)
    for frame in frames:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(frame)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "Cash flow" in body_text or "Recent transactions" in body_text or "History sync paused" in body_text:
                return True
        except Exception:
            continue
    driver.switch_to.default_content()
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return "Cash flow" in body_text or "Recent transactions" in body_text or "History sync paused" in body_text
    except Exception:
        return False


def finance_panel_ready(driver) -> bool:
    return locate_finance_frame(driver)


def unique_element(driver, selector: str):
    matches = driver.find_elements(By.CSS_SELECTOR, selector)
    if len(matches) != 1:
        raise RuntimeError("Selector must match exactly one element.")
    return matches[0]


def safe_screenshot_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.png", name):
        raise ValueError("Screenshot name must be a simple PNG filename.")
    return name


def validate_apply(actions: list[dict], apply: bool):
    if not apply and any(action["type"] in MUTATING_ACTIONS for action in actions):
        raise ValueError("Actions include possible writes; rerun with --apply after reviewing the action list.")


def execute_action(driver, action: dict, timeout: int, output_dir: Path) -> dict:
    action_type = action["type"]
    selector = action.get("selector")
    if action_type == "screenshot":
        name = safe_screenshot_name(action.get("name", "finance-dashboard-step.png"))
        path = output_dir / name
        if not driver.save_screenshot(str(path)):
            raise RuntimeError("Browser failed to save screenshot.")
        return {"type": action_type, "ok": True, "screenshot": str(path)}

    if action_type == "wait":
        WebDriverWait(driver, timeout).until(EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
        return {"type": action_type, "ok": True}

    if action_type == "count":
        return {"type": action_type, "ok": True, "matches": len(driver.find_elements(By.CSS_SELECTOR, selector))}

    element = unique_element(driver, selector)
    if action_type == "click":
        WebDriverWait(driver, timeout).until(lambda _driver: element.is_displayed() and element.is_enabled())
        element.click()
    elif action_type == "fill":
        element.clear()
        element.send_keys(action["value"])
    elif action_type == "select":
        Select(element).select_by_value(action["value"])
    elif action_type == "keypress":
        element.send_keys(KEYS[action["key"]])
    return {"type": action_type, "ok": True}


def main() -> int:
    args = parse_args()
    if args.timeout < 1 or args.timeout > 120 or args.width < 320 or args.width > 3840 or args.height < 240 or args.height > 2160:
        print("Viewport or timeout is outside allowed bounds.", file=sys.stderr)
        return 2
    try:
        actions = load_actions(args.actions_file)
        validate_apply(actions, args.apply)
        screenshot_name = safe_screenshot_name(args.screenshot_name)
        ha_url = os.environ.get("HA_URL", "").rstrip("/")
        token = os.environ.get("HA_TOKEN", "")
        if not ha_url or not token:
            raise RuntimeError("HA_URL and HA_TOKEN are required; load them from the ignored .env file.")

        token_data = {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 31536000,
            "refresh_token": None,
            "hassUrl": ha_url,
            "clientId": ha_url + "/",
            "expires": int(time.time() * 1000) + 365 * 24 * 60 * 60 * 1000,
        }
        token_literal = json.dumps(json.dumps(token_data, separators=(",", ":")))

        options = Options()
        options.binary_location = "/usr/bin/chromium"
        options.page_load_strategy = "none"
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        browser_profile = tempfile.TemporaryDirectory(prefix="finance-browser-", dir=str(artifact_dir()))
        options.add_argument(f"--user-data-dir={browser_profile.name}")
        driver = None
        output_dir = artifact_dir()
        action_results = []
        try:
            driver = webdriver.Chrome(options=options)
            driver.set_window_size(args.width, args.height)
            driver.set_page_load_timeout(args.timeout)
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": f"window.localStorage.setItem('hassTokens', {token_literal});",
            })
            driver.get(ha_url + PANEL_PATH)
            wait = WebDriverWait(driver, args.timeout)
            wait.until(finance_panel_ready)
            if not locate_finance_frame(driver):
                diagnostics = driver.execute_script("""
                    const body = document.body?.innerText || '';
                    let tokenSeeded = false;
                    try { tokenSeeded = Boolean(localStorage.getItem('hassTokens')); } catch (_) {}
                    return {
                      tokenSeeded,
                      loginPrompt: /log in|sign in/i.test(body),
                      financeText: /Finance Dashboard|Cash flow/.test(body),
                      iframeCount: document.querySelectorAll('iframe').length,
                      title: document.title.slice(0, 80)
                    };
                """)
                raise RuntimeError("Authenticated Finance Ingress panel did not render: " + json.dumps(diagnostics, separators=(",", ":")))
            for index, action in enumerate(actions, start=1):
                try:
                    action_results.append(execute_action(driver, action, args.timeout, output_dir))
                except Exception as exc:
                    raise RuntimeError(f"Action {index} ({action['type']}) failed ({type(exc).__name__}).") from None
            driver.switch_to.default_content()
            screenshot_path = output_dir / screenshot_name
            if not driver.save_screenshot(str(screenshot_path)):
                raise RuntimeError("Browser failed to save the final screenshot.")
            print(json.dumps({
                "ok": True,
                "title": driver.title,
                "panel_path": PANEL_PATH,
                "applied": bool(args.apply),
                "actions": action_results,
                "screenshot": str(screenshot_path),
            }, ensure_ascii=False, indent=2))
            return 0
        finally:
            if driver is not None:
                driver.quit()
            browser_profile.cleanup()
    except Exception as exc:
        print(f"Finance Selenium failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
