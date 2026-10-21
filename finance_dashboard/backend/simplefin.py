import base64
import requests
import json
import logging
from datetime import datetime, timezone
from database import (
    save_simplefin_data,
    get_setting,
    set_setting,
    get_account_count,
    get_transaction_count,
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
    A SimpleFIN setup token is a base64 encoded claim URL.
    Making a POST request to that claim URL exchanges it for the actual Access Token (also base64 encoded URL).
    """
    try:
        setup_token = setup_token.strip()
        if not setup_token:
            raise ValueError("Setup token cannot be empty.")

        claim_url = base64.b64decode(setup_token).decode("utf-8")
        logger.info(f"Attempting to claim SimpleFIN token from: {claim_url}")

        response = requests.post(claim_url, timeout=30)
        response.raise_for_status()

        access_url = response.text.strip()
        logger.info("Successfully received SimpleFIN access URL.")

        access_token = base64.b64encode(access_url.encode("utf-8")).decode("utf-8")
        set_simplefin_token(access_token)
        logger.info("SimpleFIN access token saved.")
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


def fetch_and_sync_simplefin():
    """Fetch normalized financial data from SimpleFIN and persist to DB."""
    token = get_simplefin_token()

    if token and token != "":
        try:
            decoded_url = base64.b64decode(token).decode("utf-8")
            logger.info(f"Fetching accounts from SimpleFIN API: {decoded_url}/accounts")
            response = requests.get(f"{decoded_url}/accounts", timeout=30)
            response.raise_for_status()
            data = response.json()
            save_simplefin_data(data)
            message = "Synced successfully from SimpleFIN"
            _record_sync_state("success", message)
            logger.info("SimpleFIN data synced and saved to DB.")
            return {
                "status": "success",
                "message": message,
                "account_count": get_account_count(),
                "transaction_count": get_transaction_count(),
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
