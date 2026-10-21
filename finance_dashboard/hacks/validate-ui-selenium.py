#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

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
    url = abs_input.as_uri()
    screenshot = Path(args.screenshot)
    screenshot.parent.mkdir(parents=True, exist_ok=True)

    options = Options()
    options.binary_location = "/usr/bin/chromium"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    driver = webdriver.Chrome(options=options)
    try:
        driver.set_page_load_timeout(args.timeout)
        driver.get(url)

        wait = WebDriverWait(driver, args.timeout)
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        wait.until(lambda d: "Finance Dashboard" in d.title)

        body_text = driver.find_element(By.TAG_NAME, "body").text
        logs = driver.get_log("browser")
        screenshot.write_bytes(driver.get_screenshot_as_png())

        locked = "PIN required" in body_text
        if not locked:
            # If the dashboard is unlocked, exercise the recurring screen directly.
            menu_button = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'button[aria-label="Open menu"]')))
            menu_button.click()
            recurring_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Recurring']]")))
            recurring_button.click()
            wait.until(lambda d: "Recurring payments" in d.find_element(By.TAG_NAME, "body").text)
            body_text = driver.find_element(By.TAG_NAME, "body").text

        body_text_length = len(body_text)
        ok = (
            "Finance Dashboard" in driver.title
            and body_text_length > 50
            and (
                (locked and "PIN required" in body_text)
                or (not locked and "Recurring payments" in body_text)
            )
        )

        result = {
            "ok": ok,
            "title": driver.title,
            "body_length": len(body_text),
            "screenshot": str(screenshot),
            "browser_logs": len(logs),
            "locked": locked,
        }
        print(json.dumps(result, indent=2))
        if not result["ok"]:
            raise SystemExit(1)
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
