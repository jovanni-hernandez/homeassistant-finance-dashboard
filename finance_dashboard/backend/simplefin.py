import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import requests

from database import (
    get_account_count,
    get_category_rules_value,
    get_stored_accounts,
    get_stored_transactions,
    get_setting,
    get_transaction_count,
    save_simplefin_data,
    set_setting,
)
from models import ACCOUNT_TYPE_UNCLASSIFIED, ACCOUNT_TYPE_VALUES

logger = logging.getLogger("finance_dashboard")

OPTIONS_PATH = "/data/options.json"
ACCESS_URL_KEY = "simplefin_access_url"
SYNC_HISTORY_DAYS = 1
SYNC_OVERLAP_DAYS = 0
SYNC_INTERVAL_SECONDS = 6 * 60 * 60
SYNC_MAX_BACKFILL_WINDOWS_PER_RUN = 7
SYNC_CURSOR_SETTING_KEY = "simplefin_sync_cursor"
ACCOUNT_BACKFILL_SETTING_KEY = "simplefin_account_backfill_state"
ACCOUNT_BACKFILL_DAYS = 90
ACCOUNT_BACKFILL_BATCH_WINDOWS = 7
SYNC_WINDOW_SECONDS = (SYNC_HISTORY_DAYS + SYNC_OVERLAP_DAYS) * 24 * 60 * 60

DEFAULT_CATEGORY_RULES = [
    {"category": "Transfers", "patterns": ["transfer", "zelle", "venmo", "cash app", "cashapp", "payment to self", "ach xfer", "ach transfer", "external transfer", "internal transfer", "credit card payment"]},
    {"category": "Income", "patterns": ["payroll", "salary", "direct deposit", "paycheck", "ach credit"]},
    {"category": "Groceries", "patterns": ["grocery", "supermarket", "food lion", "whole foods", "trader joe", "costco", "aldi", "kroger", "walmart", "sprouts"]},
    {"category": "Pets", "patterns": ["aspca pet insurance", "banfield", "pet insurance", "veterinary", "vet clinic", "pet hospital", "petco", "petsmart", "chewy"]},
    {"category": "Childcare", "patterns": ["ivymount school", "childcare", "child care", "daycare", "day care", "aftercare", "babysitter", "preschool"]},
    {"category": "Dining", "patterns": ["restaurant", "coffee", "cafe", "mcdonald", "chipotle", "starbucks", "doordash", "uber eats", "ubereats", "grubhub", "pizza"]},
    {"category": "Housing", "patterns": ["rent", "mortgage", "property", "hoa", "landlord", "apartment", "rosegate"]},
    {"category": "Utilities", "patterns": ["electric", "water", "natural gas", "internet", "phone", "utility", "spectrum", "verizon", "xfinity", "att"]},
    {"category": "Transport", "patterns": ["gas station", "fuel", "uber", "lyft", "parking", "toll", "metro", "transit", "shell", "chevron", "exxon"]},
    {"category": "Shopping", "patterns": ["amazon", "target", "costco", "shopping", "store", "walmart", "best buy", "ebay"]},
    {"category": "Travel", "patterns": ["hotel", "airlines", "flight", "travel", "rental car", "car rental", "airbnb", "amtrak"]},
    {"category": "Subscriptions", "patterns": ["netflix", "spotify", "subscription", "prime", "hulu", "apple", "youtube", "google"]},
    {"category": "Fees", "patterns": ["fee", "interest", "late", "overdraft", "atm", "service charge"]},
]


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
    # Claim URLs contain the setup credential in their path; never log them.
    logger.info("Claiming SimpleFIN setup token.")
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
        safe_message = str(e) if isinstance(e, ValueError) else "Could not claim the SimpleFIN setup token. Check the token and network, then retry."
        logger.error("Failed to claim SimpleFIN setup token (%s).", type(e).__name__)
        _record_sync_state("error", safe_message)
        return {"status": "error", "message": safe_message}


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


def _load_category_rules():
    raw = get_category_rules_value()
    if not raw:
        return DEFAULT_CATEGORY_RULES

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list) and parsed:
            normalized = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                if item.get("enabled") is False:
                    continue
                category = str(item.get("category", "")).strip()
                patterns = item.get("patterns", [])
                if category and isinstance(patterns, list):
                    normalized.append(
                        {
                            "category": category,
                            "patterns": [str(pattern).strip().lower() for pattern in patterns if str(pattern).strip()],
                        }
                    )
            if normalized:
                return normalized
    except Exception as exc:
        logger.warning(f"Could not parse category rules JSON, using defaults: {exc}")

    return DEFAULT_CATEGORY_RULES


def categorize_transaction(tx: dict, rules=None):
    haystack = f"{tx.get('payee', '')} {tx.get('description', '')}".lower()
    if "rosegate" in haystack:
        return "Housing"
    for rule in rules or DEFAULT_CATEGORY_RULES:
        category = rule.get("category")
        patterns = rule.get("patterns", [])
        if category and any(pattern in haystack for pattern in patterns):
            return category
    return "Uncategorized"


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


def suggest_account_type(account: dict):
    """Suggest a type only when provider metadata or account naming is explicit."""
    org_raw = account.get("org", {})
    org_name = org_raw.get("name", "") if isinstance(org_raw, dict) else str(org_raw or "")
    name = str(account.get("name") or account.get("title") or "")

    metadata = [account.get(key) for key in ("account_type", "type", "subtype")]
    extra = account.get("extra")
    if isinstance(extra, dict):
        metadata.extend(extra.get(key) for key in ("account_type", "account-type", "type", "subtype"))

    def normalized(value):
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    def classify(text):
        if re.search(r"\bcredit card\b|\bcreditcard\b", text):
            return "credit_card"
        if re.search(r"\b(loan|mortgage|line of credit)\b", text):
            return "loan"
        if re.search(r"\b(investment|brokerage|retirement|401k|403b|ira|roth|portfolio)\b", text):
            return "investment"
        if re.search(r"\b(checkings?|savings?|cash|deposit|bank account|depository)\b", text):
            return "bank_account"
        return ACCOUNT_TYPE_UNCLASSIFIED

    for value in metadata:
        suggestion = classify(normalized(value))
        if suggestion != ACCOUNT_TYPE_UNCLASSIFIED:
            return suggestion

    normalized_name = normalized(name)
    normalized_org = normalized(org_name)
    if "capital one" in normalized_org and re.search(r"\bventure\b", normalized_name):
        return "credit_card"
    suggestion = classify(normalized_name)
    return suggestion if suggestion in ACCOUNT_TYPE_VALUES else ACCOUNT_TYPE_UNCLASSIFIED


def _normalize_simplefin_payload(data: dict, rules=None):
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
                    "category": categorize_transaction({"payee": payee, "description": description}, rules),
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
                "account_type_suggestion": suggest_account_type(account),
                "transactions": normalized_transactions,
            }
        )

    return {"accounts": accounts}


def _fetch_simplefin_payload(access_url: str, start_ts: int | None = None, end_ts: int | None = None):
    base_url, auth = _decode_access_url_to_request(access_url)
    accounts_url = f"{base_url}/accounts"

    params = {"version": "2"}
    if start_ts is not None:
        params["start-date"] = int(start_ts)
    if end_ts is not None:
        params["end-date"] = int(end_ts)

    url_desc = f"{accounts_url}?version=2"
    if start_ts is not None and end_ts is not None:
        url_desc += f"&start-date={int(start_ts)}&end-date={int(end_ts)}"

    logger.info(f"Fetching accounts from SimpleFIN API: {url_desc}")
    response = requests.get(accounts_url, auth=auth, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def _transaction_date_summary(transactions):
    timestamps = []
    for transaction in transactions:
        try:
            timestamps.append(int(transaction.get("date")))
        except (TypeError, ValueError):
            continue

    newest_timestamp = max(timestamps, default=None)
    newest_date = (
        datetime.fromtimestamp(newest_timestamp, timezone.utc).date().isoformat()
        if newest_timestamp is not None
        else None
    )
    return len(transactions), newest_timestamp, newest_date


def _stored_latest_transaction_timestamp():
    latest = get_stored_transactions(limit=1)
    if not latest:
        return None
    try:
        return int(latest[0].get("date"))
    except (TypeError, ValueError):
        return None


def _get_sync_cursor(now_timestamp: int):
    stored_cursor = get_setting(SYNC_CURSOR_SETTING_KEY)
    try:
        cursor = int(stored_cursor)
    except (TypeError, ValueError):
        cursor = _stored_latest_transaction_timestamp()
        if cursor is None:
            cursor = now_timestamp - SYNC_WINDOW_SECONDS

    if cursor < 0 or cursor > now_timestamp:
        return max(0, now_timestamp - SYNC_WINDOW_SECONDS)
    return cursor


def _load_account_backfill_state():
    raw = get_setting(ACCOUNT_BACKFILL_SETTING_KEY)
    if not raw:
        return {}
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Could not parse new-account backfill state; starting a fresh queue.")
        return {}

    if not isinstance(state, dict):
        return {}

    normalized = {}
    for account_id, progress in state.items():
        if not isinstance(progress, dict):
            continue
        try:
            cursor = int(progress["cursor"])
            end = int(progress["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= cursor < end:
            normalized[str(account_id)] = {"cursor": cursor, "end": end}
    return normalized


def _save_account_backfill_state(state):
    set_setting(ACCOUNT_BACKFILL_SETTING_KEY, json.dumps(state, sort_keys=True))


def fetch_and_sync_simplefin():
    """Fetch normalized financial data from SimpleFIN and persist to DB."""
    access_url = get_simplefin_token()

    if access_url and access_url != "":
        try:
            now_timestamp = int(datetime.now(timezone.utc).timestamp())
            backfill_limit = max(0, now_timestamp - SYNC_WINDOW_SECONDS)
            cursor = _get_sync_cursor(now_timestamp)
            simplefin_errors = []
            rules = _load_category_rules()
            known_account_ids = {
                str(account.get("id"))
                for account in get_stored_accounts()
                if account.get("id") is not None
            }
            account_backfill_state = _load_account_backfill_state()
            previous_latest_timestamp = _stored_latest_transaction_timestamp()
            previous_latest_date = (
                datetime.fromtimestamp(previous_latest_timestamp, timezone.utc).date().isoformat()
                if previous_latest_timestamp is not None
                else None
            )

            incoming_transactions_by_id = {}
            backfill_windows = 0

            def save_normalized_payload(normalized):
                discovered_new_account = False
                account_backfill_end = max(0, now_timestamp - SYNC_WINDOW_SECONDS)
                account_backfill_start = max(
                    0,
                    now_timestamp - ACCOUNT_BACKFILL_DAYS * SYNC_WINDOW_SECONDS,
                )
                for account in normalized.get("accounts", []):
                    account_id = str(account.get("id"))
                    if account_id in known_account_ids:
                        continue
                    known_account_ids.add(account_id)
                    if account_id not in account_backfill_state and account_backfill_start < account_backfill_end:
                        account_backfill_state[account_id] = {
                            "cursor": account_backfill_start,
                            "end": account_backfill_end,
                        }
                        discovered_new_account = True

                if discovered_new_account:
                    _save_account_backfill_state(account_backfill_state)

                for account in normalized.get("accounts", []):
                    for transaction in account.get("transactions", []):
                        incoming_transactions_by_id[str(transaction.get("id"))] = transaction
                save_simplefin_data(normalized)

            while cursor < backfill_limit and backfill_windows < SYNC_MAX_BACKFILL_WINDOWS_PER_RUN:
                window_end = min(cursor + SYNC_WINDOW_SECONDS, backfill_limit)
                payload = _fetch_simplefin_payload(access_url, start_ts=cursor, end_ts=window_end)
                for key in ("errlist", "errors"):
                    if payload.get(key):
                        simplefin_errors.extend(
                            payload.get(key) if isinstance(payload.get(key), list) else [payload.get(key)]
                        )

                normalized = _normalize_simplefin_payload(payload, rules)
                save_normalized_payload(normalized)

                cursor = window_end
                set_setting(SYNC_CURSOR_SETTING_KEY, str(cursor))
                backfill_windows += 1

            backfill_pending = cursor < backfill_limit
            remaining_backfill_days = (
                (backfill_limit - cursor + SYNC_WINDOW_SECONDS - 1) // SYNC_WINDOW_SECONDS
                if backfill_pending
                else 0
            )

            # Always re-fetch the latest rolling day so transactions posted late
            # within the day are still picked up after the backfill cursor advances.
            rolling_start = max(0, now_timestamp - SYNC_WINDOW_SECONDS)
            payload = _fetch_simplefin_payload(access_url, start_ts=rolling_start, end_ts=now_timestamp)
            for key in ("errlist", "errors"):
                if payload.get(key):
                    simplefin_errors.extend(
                        payload.get(key) if isinstance(payload.get(key), list) else [payload.get(key)]
                    )
            normalized = _normalize_simplefin_payload(payload, rules)
            save_normalized_payload(normalized)

            if not backfill_pending:
                cursor = now_timestamp
                set_setting(SYNC_CURSOR_SETTING_KEY, str(cursor))

            account_backfill_windows = 0
            while account_backfill_state:
                batch_windows = 0
                while account_backfill_state and batch_windows < ACCOUNT_BACKFILL_BATCH_WINDOWS:
                    account_id = min(
                        account_backfill_state,
                        key=lambda key: (
                            account_backfill_state[key]["cursor"],
                            account_backfill_state[key]["end"],
                            key,
                        ),
                    )
                    cursor = account_backfill_state[account_id]["cursor"]
                    window_end = min(
                        cursor + SYNC_WINDOW_SECONDS,
                        account_backfill_state[account_id]["end"],
                    )
                    group = [
                        key for key, progress in account_backfill_state.items()
                        if progress["cursor"] == cursor and progress["end"] == account_backfill_state[account_id]["end"]
                    ]

                    payload = _fetch_simplefin_payload(access_url, start_ts=cursor, end_ts=window_end)
                    for key in ("errlist", "errors"):
                        if payload.get(key):
                            simplefin_errors.extend(
                                payload.get(key) if isinstance(payload.get(key), list) else [payload.get(key)]
                            )
                    normalized = _normalize_simplefin_payload(payload, rules)
                    save_normalized_payload(normalized)

                    for queued_account_id in group:
                        progress = account_backfill_state.get(queued_account_id)
                        if not progress:
                            continue
                        if window_end >= progress["end"]:
                            account_backfill_state.pop(queued_account_id, None)
                        else:
                            progress["cursor"] = window_end
                    _save_account_backfill_state(account_backfill_state)
                    account_backfill_windows += 1
                    batch_windows += 1

            account_backfill_pending = bool(account_backfill_state)
            account_backfill_remaining_days = sum(
                (progress["end"] - progress["cursor"] + SYNC_WINDOW_SECONDS - 1) // SYNC_WINDOW_SECONDS
                for progress in account_backfill_state.values()
            )

            incoming_transactions = list(incoming_transactions_by_id.values())
            incoming_count, newest_incoming_timestamp, newest_incoming_date = _transaction_date_summary(
                incoming_transactions
            )
            newest_date_advanced = newest_incoming_timestamp is not None and (
                previous_latest_timestamp is None or newest_incoming_timestamp > previous_latest_timestamp
            )

            total_windows = backfill_windows + 1 + account_backfill_windows
            message = (
                f"SimpleFIN request completed in {total_windows} window(s), each no longer than "
                f"{SYNC_HISTORY_DAYS + SYNC_OVERLAP_DAYS} day."
            )
            if simplefin_errors:
                message += f" Provider warning: {simplefin_errors[0]}"
            if incoming_count:
                message += f" Returned {incoming_count} transactions; newest dated {newest_incoming_date or 'unknown'}."
                if newest_date_advanced:
                    message += " Freshness verified: the newest transaction date advanced."
                elif previous_latest_date:
                    message += f" Freshness NOT verified: no date newer than the previously stored latest ({previous_latest_date}) was returned."
                else:
                    message += " Freshness could not be verified because no usable transaction date was returned."
            else:
                message += " Freshness NOT verified: SimpleFIN returned no transactions."
            if backfill_pending:
                message += (
                    f" Historical backfill is incomplete: about {remaining_backfill_days} day(s) remain; "
                    "it will continue on the next sync."
                )
            if account_backfill_windows:
                message += (
                    f" New-account history completed {account_backfill_windows} additional one-day "
                    "window(s) in seven-request batches."
                )
            if account_backfill_pending:
                message += (
                    f" New-account history backfill is incomplete: about {account_backfill_remaining_days} "
                    "account-day window(s) remain; it will resume on the next sync."
                )

            _record_sync_state("success", message)
            logger.info(
                "SimpleFIN sync completed: %d windows, %d transactions returned; newest date %s; "
                "date advanced %s; backfill pending %s (%d days).",
                total_windows,
                incoming_count,
                newest_incoming_date or "unknown",
                newest_date_advanced,
                backfill_pending,
                remaining_backfill_days,
            )
            return {
                "status": "success",
                "message": message,
                "account_count": get_account_count(),
                "transaction_count": get_transaction_count(),
                "incoming_transaction_count": incoming_count,
                "latest_incoming_transaction_date": newest_incoming_date,
                "previous_latest_transaction_date": previous_latest_date,
                "newest_transaction_date_advanced": newest_date_advanced,
                "sync_windows": total_windows,
                "backfill_pending": backfill_pending,
                "backfill_remaining_days": remaining_backfill_days,
                "account_backfill_pending": account_backfill_pending,
                "account_backfill_remaining_days": account_backfill_remaining_days,
                "account_backfill_windows": account_backfill_windows,
                "warnings": simplefin_errors,
            }
        except Exception as e:
            logger.error(f"Error fetching SimpleFIN data: {e}")
            message = f"Sync failed: {e}"
            saved_backfills = locals().get("account_backfill_state", {})
            if saved_backfills:
                message += (
                    f" New-account backfill progress is saved for {len(saved_backfills)} account(s) "
                    "and will resume on the next sync."
                )
            _record_sync_state("error", message)
            return {"status": "error", "message": message}

    logger.warning("No SimpleFIN access URL found. Seeding mock data.")
    mock_data = {
        "accounts": [
            {
                "id": "1",
                "org": {"name": "Capital One"},
                "name": "Venture X",
                "balance": "1250.00",
                "currency": "USD",
                "account_type_suggestion": "credit_card",
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
                "account_type_suggestion": "investment",
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
                "account_type_suggestion": "bank_account",
                "transactions": [],
            },
            {
                "id": "4",
                "org": {"name": "Fidelity"},
                "name": "Brokerage",
                "balance": "45200.00",
                "currency": "USD",
                "account_type_suggestion": "investment",
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
