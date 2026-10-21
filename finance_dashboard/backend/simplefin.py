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


def _record_sync_state(status: str, message: str):
    set_setting("last_sync_status", status)
    set_setting("last_sync_message", message)
    set_setting("last_sync_at", datetime.now(timezone.utc).isoformat())


def get_simplefin_token():
    """Prioritize fetching token from database, then add-on options."""
    try:
        db_token = get_setting("simplefin_access_token")
        if db_token:
            logger.info("SimpleFIN access token retrieved from DB.")
            return db_token
    except Exception as e:
        logger.warning(f"Could not retrieve SimpleFIN token from DB (initial attempt): {e}")

    try:
        with open(OPTIONS_PATH) as f:
            options = json.load(f)
            token = options.get("simplefin_token")
            if token and token.strip():
                logger.info("SimpleFIN token retrieved from options.json.")
                return token.strip()
    except Exception as e:
        logger.warning(f"Could not retrieve SimpleFIN token from options.json: {e}")

    logger.info("SimpleFIN access token not found in DB or options.json.")
    return None


def set_simplefin_token(token: str):
    """Save SimpleFIN access token securely to the database settings."""
    logger.info("Saving SimpleFIN access token to DB...")
    set_setting("simplefin_access_token", token.strip())
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

        claim_url = base64.b64decode(setup_token).decode("utf-8")
        logger.info(f"Attempting to claim SimpleFIN token from: {claim_url}")

        response = requests.post(claim_url, headers={"Content-Length": "0"}, timeout=30)
        response.raise_for_status()

        access_url = response.text.strip()
        if not access_url:
            raise ValueError("SimpleFIN did not return an access URL.")

        logger.info("Successfully received SimpleFIN access URL.")
        access_token = base64.b64encode(access_url.encode("utf-8")).decode("utf-8")
        set_simplefin_token(access_token)
        logger.info("SimpleFIN access token saved.")
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


def _decode_access_token_to_request(token: str):
    access_url = base64.b64decode(token).decode("utf-8")
    parsed = urlsplit(access_url)

    if not parsed.scheme or not parsed.hostname:
        raise ValueError("Invalid SimpleFIN access URL.")

    username = parsed.username or ""
    password = parsed.password or ""
    netloc = parsed.hostname
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"

    base_url = urlunsplit((parsed.scheme, netloc, "", "", ""))
    return base_url, (username, password)


def _normalize_simplefin_payload(data: dict):
    accounts = []
    for account in data.get("accounts", []):
        account_id = str(account.get("id") or account.get("account") or account.get("name") or hashlib.sha256(repr(account).encode()).hexdigest()[:16])
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
    token = get_simplefin_token()

    if token and token != "":
        try:
            base_url, auth = _decode_access_token_to_request(token)
            accounts_url = f"{base_url}/accounts"
            logger.info(f"Fetching accounts from SimpleFIN API: {accounts_url}?version=2")

            response = requests.get(accounts_url, auth=auth, params={"version": "2"}, timeout=30)
            response.raise_for_status()
            payload = response.json()

            warnings = []
            for key in ("errlist", "errors"):
                if payload.get(key):
                    warnings.extend(payload.get(key) if isinstance(payload.get(key), list) else [payload.get(key)])

            normalized = _normalize_simplefin_payload(payload)
            save_simplefin_data(normalized)

            message = "Synced successfully from SimpleFIN"
            if warnings:
                message = f"Synced with warnings: {warnings[0]}"

            _record_sync_state("success", message)
            logger.info("SimpleFIN data synced and saved to DB.")
            return {
                "status": "success",
                "message": message,
                "account_count": get_account_count(),
                "transaction_count": get_transaction_count(),
                "warnings": warnings,
            }
        except Exception as e:
            logger.error(f"Error fetching SimpleFIN data: {e}")
            _record_sync_state("error", f"Sync failed: {e}")
            return {"status": "error", "message": str(e)}

    logger.warning("No SimpleFIN access token found. Seeding mock data.")
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
