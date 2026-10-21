import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

from database import (
    get_account_count,
    get_setting,
    get_transaction_count,
    save_simplefin_data,
    set_setting,
)

logger = logging.getLogger("finance_dashboard")

OPTIONS_PATH = "/data/options.json"
ACCESS_URL_KEY = "simplefin_access_url"


def _record_sync_state(status: str, message: str):
    set_setting("last_sync_status", status)
    set_setting("last_sync_message", message)
    set_setting("last_sync_at", datetime.now(timezone.utc).isoformat())


def _looks_like_access_url(value: str) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://")) and "@" in value


def _decode_base64(text: str):
    try:
        return base64.b64decode(text).decode("utf-8").strip()
    except Exception:
        return None


def _store_access_url(access_url: str):
    if not access_url:
        raise ValueError("Empty SimpleFIN access URL.")
    set_setting(ACCESS_URL_KEY, access_url.strip())


def _claim_setup_url(claim_url: str):
    logger.info(f"Claiming SimpleFIN setup token via: {claim_url}")
    response = requests.post(claim_url, headers={"Content-Length": "0"}, timeout=30)
    response.raise_for_status()
    access_url = response.text.strip()
    if not _looks_like_access_url(access_url):
        raise ValueError("SimpleFIN did not return a valid access URL.")
    return access_url


def get_simplefin_token():
    """Return the raw SimpleFIN access URL."""
    try:
        stored = get_setting(ACCESS_URL_KEY)
        if stored and _looks_like_access_url(stored):
            logger.info("SimpleFIN access URL retrieved from DB.")
            return stored
    except Exception as e:
        logger.warning(f"Could not retrieve SimpleFIN access URL from DB: {e}")

    try:
        with open(OPTIONS_PATH) as f:
            options = json.load(f)
            candidate = (options.get("simplefin_token") or "").strip()
            if candidate:
                logger.info("SimpleFIN candidate retrieved from options.json.")
                resolved = _resolve_candidate(candidate)
                if resolved:
                    _store_access_url(resolved)
                    return resolved
    except Exception as e:
        logger.warning(f"Could not retrieve SimpleFIN token from options.json: {e}")

    logger.info("SimpleFIN access URL not found in DB or options.json.")
    return None


def _resolve_candidate(candidate: str):
    """Resolve a candidate from options.json into an access URL.

    Accepts either:
    - raw access URL (preferred)
    - base64-encoded setup token (claim URL)
    """
    if not candidate:
        return None

    candidate = candidate.strip()
    if not candidate:
        return None

    if _looks_like_access_url(candidate):
        return candidate

    decoded = _decode_base64(candidate)
    if not decoded or not decoded.startswith(("http://", "https://")):
        return None

    # If it's a claim URL, claim it once and store the resulting access URL.
    if "/claim/" in decoded or decoded.rstrip("/").endswith("/claim"):
        return _claim_setup_url(decoded)

    # If it already looks like an access URL after decoding, accept it.
    if _looks_like_access_url(decoded):
        return decoded

    return None


def set_simplefin_token(token: str):
    """Save a raw SimpleFIN access URL securely to the database settings."""
    logger.info("Saving SimpleFIN access URL to DB...")
    _store_access_url(token)
    return True


def claim_simplefin_setup_token(setup_token: str):
    """
    Claim a SimpleFIN setup token.
    The setup token is a base64 encoded claim URL.
    A POST to that URL returns the persistent access URL.
    """
    try:
        setup_token = setup_token.strip()
        if not setup_token:
            raise ValueError("Setup token cannot be empty.")

        claim_url = _decode_base64(setup_token)
        if not claim_url or not claim_url.startswith(("http://", "https://")):
            raise ValueError("Invalid SimpleFIN setup token.")

        access_url = _claim_setup_url(claim_url)
        logger.info("Successfully received SimpleFIN access URL.")
        _store_access_url(access_url)
        logger.info("SimpleFIN access URL saved.")
        _record_sync_state("success", "SimpleFIN token claimed successfully")
        return {"status": "success", "message": "Successfully claimed SimpleFIN token"}
    except Exception as e:
        logger.error(f"Failed to claim SimpleFIN setup token: {e}")
        _record_sync_state("error", f"Failed to claim token: {e}")
        return {"status": "error", "message": str(e)}


def get_dashboard_status():
    token = get_simplefin_token()
    return {
        "connected": bool(token),
        "account_count": get_account_count(),
        "transaction_count": get_transaction_count(),
        "last_sync_status": get_setting("last_sync_status") or "never",
        "last_sync_message": get_setting("last_sync_message") or "No sync has completed yet.",
        "last_sync_at": get_setting("last_sync_at"),
        "has_token": bool(token),
    }


def _decode_access_url_to_request(access_url: str):
    parsed = urlsplit(access_url)

    if not parsed.scheme or not parsed.hostname:
        raise ValueError("Invalid SimpleFIN access URL.")

    username = parsed.username or ""
    password = parsed.password or ""
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    base_path = parsed.path.rstrip("/")
    base_url = urlunsplit((parsed.scheme, netloc, base_path, "", ""))
    return base_url, (username, password)


def _normalize_simplefin_payload(data: dict):
    accounts = []
    for account in data.get("accounts", []):
        account_id = str(
            account.get("id")
            or account.get("account")
            or account.get("name")
            or hashlib.sha256(repr(account).encode()).hexdigest()[:16]
        )
        org_raw = account.get("org", {})
        if isinstance(org_raw, dict):
            org = org_raw.get("name") or org_raw.get("id") or "Unknown"
        else:
            org = str(org_raw or "Unknown")

        normalized_transactions = []
        for tx in account.get("transactions", []):
            posted = tx.get("posted") or tx.get("date")
            description = tx.get("description") or tx.get("memo") or tx.get("name") or ""
            amount = tx.get("amount", "0.00")
            payee = tx.get("payee") or tx.get("merchant") or tx.get("name") or description
            tx_id = str(
                tx.get("id")
                or tx.get("transaction_id")
                or hashlib.sha256(f"{account_id}:{posted}:{amount}:{description}".encode()).hexdigest()[:24]
            )

            normalized_transactions.append(
                {
                    "id": tx_id,
                    "date": posted,
                    "amount": str(amount),
                    "payee": payee,
                    "description": description,
                    "pending": bool(tx.get("pending", False)),
                }
            )

        accounts.append(
            {
                "id": account_id,
                "org": {"name": org},
                "name": account.get("name") or account.get("title") or "Account",
                "balance": str(account.get("balance", "0.00")),
                "currency": account.get("currency", "USD"),
                "transactions": normalized_transactions,
            }
        )

    return {"accounts": accounts}


def fetch_and_sync_simplefin():
    """Fetch normalized financial data from SimpleFIN and persist to DB."""
    access_url = get_simplefin_token()

    if access_url and access_url != "":
        try:
            base_url, auth = _decode_access_url_to_request(access_url)
            accounts_url = f"{base_url}/accounts"
            logger.info(f"Fetching accounts from SimpleFIN API: {accounts_url}?version=2")

            response = requests.get(accounts_url, auth=auth, params={"version": "2"}, timeout=30)
            response.raise_for_status()
            payload = response.json()

            simplefin_errors = []
            for key in ("errlist", "errors"):
                if payload.get(key):
                    simplefin_errors.extend(payload.get(key) if isinstance(payload.get(key), list) else [payload.get(key)])

            normalized = _normalize_simplefin_payload(payload)
            save_simplefin_data(normalized)

            message = "Synced successfully from SimpleFIN"
            if simplefin_errors:
                message = f"Synced with warnings: {simplefin_errors[0]}"

            _record_sync_state("success", message)
            logger.info("SimpleFIN data synced and saved to DB.")
            return {
                "status": "success",
                "message": message,
                "account_count": get_account_count(),
                "transaction_count": get_transaction_count(),
                "warnings": simplefin_errors,
            }
        except Exception as e:
            logger.error(f"Error fetching SimpleFIN data: {e}")
            _record_sync_state("error", f"Sync failed: {e}")
            return {"status": "error", "message": str(e)}

    logger.warning("No SimpleFIN access URL found. Seeding mock data.")
    mock_data = {
        "accounts": [
            {
                "id": "1",
                "org": {"name": "Capital One"},
                "name": "Venture X",
                "balance": "1250.00",
                "currency": "USD",
                "transactions": [
                    {"id": "tx1", "date": 1718500000, "amount": "-45.50", "payee": "Grocery Store", "description": "Weekly groceries", "pending": False},
                    {"id": "tx2", "date": 1718400000, "amount": "-12.00", "payee": "Coffee Shop", "description": "Latte", "pending": False},
                ],
            },
            {
                "id": "2",
                "org": {"name": "TSP"},
                "name": "L Fund 2050",
                "balance": "125000.00",
                "currency": "USD",
                "transactions": [
                    {"id": "tx3", "date": 1718000000, "amount": "500.00", "payee": "Payroll Deposit", "description": "Bi-weekly contribution", "pending": False}
                ],
            },
            {
                "id": "3",
                "org": {"name": "Navy Federal"},
                "name": "Checking",
                "balance": "3400.50",
                "currency": "USD",
                "transactions": [],
            },
            {
                "id": "4",
                "org": {"name": "Fidelity"},
                "name": "Brokerage",
                "balance": "45200.00",
                "currency": "USD",
                "transactions": [],
            },
        ]
    }
    save_simplefin_data(mock_data)
    message = "Loaded mock data"
    _record_sync_state("success", message)
    return {
        "status": "success",
        "message": message,
        "account_count": get_account_count(),
        "transaction_count": get_transaction_count(),
    }
