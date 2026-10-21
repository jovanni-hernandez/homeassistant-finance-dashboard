#!/usr/bin/env python3
"""Refresh the HA app store and update this add-on through HA's authenticated API proxy."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
DEFAULT_REPO = "https://github.com/jovanni-hernandez/homeassistant-finance-dashboard"


class ReleaseError(RuntimeError):
    pass


def read_config() -> tuple[str, str]:
    text = CONFIG.read_text()
    slug = re.search(r"^slug:\s*([a-z0-9_-]+)\s*$", text, re.M)
    version = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.M)
    if not slug or not version:
        raise ReleaseError("Could not read add-on slug/version from config.yaml")
    return slug.group(1), version.group(1)


class HAProxy:
    def __init__(self):
        self.base = os.environ.get("HA_URL", "").rstrip("/")
        self.token = os.environ.get("HA_TOKEN", "")
        if not self.base or not self.token:
            raise ReleaseError("HA_URL and HA_TOKEN are required; load them from finance_dashboard/.env or hacks/.release.env")

    def request(self, method: str, path: str, payload=None):
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            # Never echo request headers or credentials. A bounded body can include
            # the useful Supervisor error without exposing the bearer token.
            message = exc.read(1024).decode("utf-8", errors="replace")
            raise ReleaseError(f"{method} {path} failed with HTTP {exc.code}: {message[:500]}") from exc
        except Exception as exc:
            raise ReleaseError(f"{method} {path} failed: {type(exc).__name__}: {exc}") from exc

        if not raw:
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReleaseError(f"{method} {path} returned non-JSON data") from exc
        if isinstance(result, dict) and result.get("result") == "error":
            raise ReleaseError(f"{method} {path} rejected by Supervisor: {result.get('message', 'unknown error')}")
        return result

    def supervisor(self, method: str, path: str, payload=None):
        envelope = self.request(method, "/api/hassio" + path, payload)
        if isinstance(envelope, dict) and envelope.get("result") == "ok":
            return envelope.get("data") or {}, envelope
        # Core's Supervisor proxy returns the Supervisor envelope unchanged.
        return envelope, envelope


def data_field(payload, key, default=None):
    if not isinstance(payload, dict):
        return default
    data = payload.get("data")
    if isinstance(data, dict) and key in data:
        return data[key]
    return payload.get(key, default)


def refresh_store(client: HAProxy, slug: str, expected_version: str, attempts: int, delay: float):
    print("Reloading the Home Assistant app store through /api/hassio/store/reload...")
    client.supervisor("POST", "/store/reload", {})

    last_seen = None
    for attempt in range(1, attempts + 1):
        info, _ = client.supervisor("GET", f"/store/addons/{slug}")
        latest = info.get("version_latest") or info.get("version")
        last_seen = latest
        if latest == expected_version:
            print(f"Store metadata reports {slug} version {expected_version}.")
            return info
        if attempt < attempts:
            wait = min(delay * (2 ** (attempt - 1)), 20)
            print(f"Store metadata is {latest or 'unknown'}; retrying in {wait:g}s ({attempt}/{attempts}).")
            time.sleep(wait)
    raise ReleaseError(f"Store did not report expected version {expected_version}; last latest version was {last_seen or 'unknown'}")


def read_installed_info(client: HAProxy, slug: str):
    info, _ = client.supervisor("GET", f"/addons/{slug}/info")
    return info


def wait_for_job(client: HAProxy, job_id: str, slug: str, expected_version: str, attempts: int, delay: float):
    for attempt in range(1, attempts + 1):
        job, _ = client.supervisor("GET", f"/jobs/{job_id}")
        if job.get("done"):
            errors = job.get("errors") or []
            if errors:
                raise ReleaseError(f"Supervisor update job failed: {errors}")
            info = read_installed_info(client, slug)
            installed = info.get("version")
            if installed != expected_version:
                raise ReleaseError(f"Update job finished, but installed version is {installed or 'unknown'} (expected {expected_version})")
            return info
        if attempt < attempts:
            wait = min(delay * (2 ** min(attempt - 1, 4)), 20)
            print(f"Update job {job_id} is still running; checking again in {wait:g}s ({attempt}/{attempts}).")
            time.sleep(wait)
    raise TimeoutError(f"Supervisor update job {job_id} did not finish after {attempts} checks")


def deploy(client: HAProxy, slug: str, version: str, attempts: int, delay: float):
    store_info = refresh_store(client, slug, version, attempts, delay)
    installed_info = read_installed_info(client, slug)
    installed = installed_info.get("version")
    if installed == version:
        print(f"Add-on already has version {version}; no update required.")
        return {"slug": slug, "installed_version": installed, "updated": False}

    latest = store_info.get("version_latest") or store_info.get("version")
    if latest != version:
        raise ReleaseError(f"Refusing update: store version {latest!r} did not match expected {version!r}")

    print(f"Requesting Supervisor update to {version} with backup disabled...")
    response, envelope = client.supervisor(
        "POST",
        f"/store/addons/{slug}/update",
        {"backup": False, "background": True},
    )
    job_id = data_field(envelope, "job_id") or data_field(response, "job_id")
    if job_id:
        installed_info = wait_for_job(client, str(job_id), slug, version, attempts, delay)
    else:
        # Some Supervisor versions wait for completion and return no job id.
        last_installed = None
        for attempt in range(1, attempts + 1):
            installed_info = read_installed_info(client, slug)
            last_installed = installed_info.get("version")
            if last_installed == version:
                break
            if attempt < attempts:
                time.sleep(min(delay * (2 ** min(attempt - 1, 4)), 20))
        else:
            raise ReleaseError(f"Update endpoint returned without a job id; installed version is {last_installed or 'unknown'}")

    installed = installed_info.get("version")
    state = installed_info.get("state")
    if installed != version:
        raise ReleaseError(f"Installed version is {installed or 'unknown'}; expected {version}")
    print(f"Supervisor confirms {slug} version {version} (state: {state or 'unknown'}).")
    return {"slug": slug, "installed_version": installed, "state": state, "updated": True, "job_id": job_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("refresh", "deploy"))
    parser.add_argument("repo", nargs="?", default=DEFAULT_REPO)
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    slug, version = read_config()
    client = HAProxy()
    if args.operation == "refresh":
        info = refresh_store(client, slug, version, args.attempts, args.delay)
        print(json.dumps({"repo": args.repo, "slug": slug, "version_latest": info.get("version_latest") or info.get("version")}, indent=2))
    else:
        result = deploy(client, slug, version, args.attempts, args.delay)
        print(json.dumps({"repo": args.repo, **result}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
