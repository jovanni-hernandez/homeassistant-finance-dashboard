import base64
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
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
from models import ACCOUNT_TYPE_UNCLASSIFIED, ACCOUNT_TYPE_VALUES, AccountDataCoverage, SessionLocal

logger = logging.getLogger("finance_dashboard")

OPTIONS_PATH = "/data/options.json"
ACCESS_URL_KEY = "simplefin_access_url"
SYNC_HISTORY_DAYS = 1
SYNC_OVERLAP_DAYS = 0
SYNC_INTERVAL_SECONDS = 6 * 60 * 60
SYNC_MAX_BACKFILL_WINDOWS_PER_RUN = 7
SYNC_CURSOR_SETTING_KEY = "simplefin_sync_cursor"
ACCOUNT_BACKFILL_SETTING_KEY = "simplefin_account_backfill_state"
ACCOUNT_BACKFILL_VERSION_KEY = "simplefin_account_backfill_version"
ACCOUNT_BACKFILL_VERSION = "12_months_v1"
ACCOUNT_BACKFILL_DAYS = 365
ACCOUNT_BACKFILL_WINDOW_DAYS = 90
ACCOUNT_BACKFILL_BATCH_WINDOWS = 7
SYNC_WINDOW_SECONDS = (SYNC_HISTORY_DAYS + SYNC_OVERLAP_DAYS) * 24 * 60 * 60
ACCOUNT_BACKFILL_WINDOW_SECONDS = ACCOUNT_BACKFILL_WINDOW_DAYS * 24 * 60 * 60
SIMPLEFIN_REQUEST_BUDGET_KEY = "simplefin_request_budget"
SIMPLEFIN_SYNC_PROGRESS_KEY = "simplefin_sync_progress"
SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY = 12
SIMPLEFIN_MIN_REQUEST_INTERVAL_SECONDS = 10
_simplefin_request_lock = threading.Lock()
_ACCOUNT_BACKFILL_DAY_SECONDS = 24 * 60 * 60


class SimpleFINDailyLimitError(RuntimeError):
    def __init__(self, message: str, *, requests_today: int, reset_at: str):
        super().__init__(message)
        self.requests_today = requests_today
        self.reset_at = reset_at


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _next_utc_reset(now: datetime | None = None) -> datetime:
    now = now or _utc_now()
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)


def _access_url_fingerprint(access_url: str) -> str:
    return hashlib.sha256(str(access_url).encode("utf-8")).hexdigest()


def _read_request_budget() -> dict | None:
    raw = get_setting(SIMPLEFIN_REQUEST_BUDGET_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _write_request_budget(state: dict):
    set_setting(SIMPLEFIN_REQUEST_BUDGET_KEY, json.dumps(state, separators=(",", ":"), sort_keys=True))


def _initialize_request_budget(access_url: str):
    """Start a fresh quota ledger only when an access URL is newly stored."""
    fingerprint = _access_url_fingerprint(access_url)
    current = _read_request_budget()
    if current and current.get("access_url_sha256") == fingerprint:
        return
    now = _utc_now()
    _write_request_budget({
        "access_url_sha256": fingerprint,
        "utc_date": now.date().isoformat(),
        "requests_today": 0,
        "last_request_at": None,
        "unknown_usage": False,
    })


def _request_budget_status(access_url: str | None = None) -> dict:
    now = _utc_now()
    if not access_url:
        return {
            "requests_today": 0,
            "daily_limit": SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
            "requests_remaining": SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
            "initialized": False,
            "unknown_usage": False,
            "utc_day_resets_at": _next_utc_reset(now).isoformat(),
        }
    state = _read_request_budget()
    fingerprint = _access_url_fingerprint(access_url)
    if not state or (fingerprint and state.get("access_url_sha256") != fingerprint):
        return {
            "requests_today": 0,
            "daily_limit": SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
            "requests_remaining": 0,
            "initialized": False,
            "unknown_usage": True,
            "utc_day_resets_at": _next_utc_reset(now).isoformat(),
        }
    if state.get("utc_date") != now.date().isoformat():
        count = 0
        unknown_usage = False
    else:
        count = max(0, min(SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY, int(state.get("requests_today", 0) or 0)))
        unknown_usage = bool(state.get("unknown_usage"))
    return {
        "requests_today": count,
        "daily_limit": SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
        "requests_remaining": max(0, SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY - count),
        "initialized": True,
        "unknown_usage": unknown_usage,
        "utc_day_resets_at": _next_utc_reset(now).isoformat(),
    }


def _reserve_simplefin_request(access_url: str) -> dict:
    """Persist a daily-budget reservation before issuing a Bridge request."""
    fingerprint = _access_url_fingerprint(access_url)
    with _simplefin_request_lock:
        while True:
            now = _utc_now()
            today = now.date().isoformat()
            state = _read_request_budget()
            if not state or state.get("access_url_sha256") != fingerprint:
                # Existing installs may already have made requests today, but
                # that history was not recorded. Fail closed for this UTC day.
                state = {
                    "access_url_sha256": fingerprint,
                    "utc_date": today,
                    "requests_today": SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
                    "last_request_at": None,
                    "unknown_usage": True,
                }
                _write_request_budget(state)
                raise SimpleFINDailyLimitError(
                    "SimpleFIN request history is not yet tracked. Sync will resume after the daily quota resets.",
                    requests_today=SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
                    reset_at=_next_utc_reset(now).isoformat(),
                )

            if state.get("utc_date") != today:
                state.update({
                    "utc_date": today,
                    "requests_today": 0,
                    "last_request_at": None,
                    "unknown_usage": False,
                })

            count = int(state.get("requests_today", 0) or 0)
            if state.get("unknown_usage") or count >= SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY:
                _write_request_budget(state)
                raise SimpleFINDailyLimitError(
                    "The conservative SimpleFIN daily request budget is exhausted. Sync will resume after the UTC day resets.",
                    requests_today=min(count, SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY),
                    reset_at=_next_utc_reset(now).isoformat(),
                )

            last_request = state.get("last_request_at")
            try:
                wait_seconds = SIMPLEFIN_MIN_REQUEST_INTERVAL_SECONDS - (now.timestamp() - float(last_request)) if last_request else 0
            except (TypeError, ValueError):
                wait_seconds = 0
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                continue

            state.update({
                "utc_date": today,
                "requests_today": count + 1,
                "last_request_at": now.timestamp(),
                "unknown_usage": False,
            })
            _write_request_budget(state)
            return _request_budget_status(access_url)


def _read_sync_progress() -> dict:
    raw = get_setting(SIMPLEFIN_SYNC_PROGRESS_KEY)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_sync_progress(**updates) -> dict:
    progress = _read_sync_progress()
    progress.update(updates)
    progress["updated_at"] = _utc_now().isoformat()
    _write = json.dumps(progress, separators=(",", ":"), sort_keys=True)
    set_setting(SIMPLEFIN_SYNC_PROGRESS_KEY, _write)
    return progress

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
    normalized = access_url.strip()
    _initialize_request_budget(normalized)
    set_setting(ACCESS_URL_KEY, normalized)


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
    progress = _read_sync_progress()
    budget = _request_budget_status(token)
    if progress:
        progress.update({
            "requests_today": budget["requests_today"],
            "daily_request_limit": budget["daily_limit"],
            "requests_remaining": budget["requests_remaining"],
            "utc_day_resets_at": budget["utc_day_resets_at"],
        })
    return {
        "connected": bool(token),
        "account_count": get_account_count(),
        "transaction_count": get_transaction_count(),
        "last_sync_status": get_setting("last_sync_status") or "never",
        "last_sync_message": get_setting("last_sync_message") or "No sync has completed yet.",
        "last_sync_at": get_setting("last_sync_at"),
        "has_token": bool(token),
        "sync_progress": progress or None,
        "simplefin_request_budget": budget,
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


def _fetch_simplefin_payload(access_url: str, start_ts: int | None = None, end_ts: int | None = None, *, on_reserved=None):
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

    budget = _reserve_simplefin_request(access_url)
    _write_sync_progress(
        requests_today=budget["requests_today"],
        daily_request_limit=budget["daily_limit"],
        requests_remaining=budget["requests_remaining"],
        utc_day_resets_at=budget["utc_day_resets_at"],
    )
    if on_reserved:
        on_reserved()
    logger.info("Fetching accounts from SimpleFIN API: %s", url_desc)
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
            start = int(progress.get("start", cursor))
            window_days = int(progress.get("window_days", ACCOUNT_BACKFILL_WINDOW_DAYS))
            retry_after = int(progress.get("retry_after", 0) or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= cursor < end:
            normalized[str(account_id)] = {
                "start": min(start, cursor),
                "cursor": cursor,
                "end": end,
                "window_days": max(1, min(window_days, ACCOUNT_BACKFILL_WINDOW_DAYS)),
                "retry_after": max(0, retry_after),
            }
    return normalized


def _save_account_backfill_state(state):
    set_setting(ACCOUNT_BACKFILL_SETTING_KEY, json.dumps(state, sort_keys=True))


def _queue_initial_account_history(account_ids, now_timestamp: int, state: dict):
    """Queue one 12-month history fill for accounts present at migration time."""
    if get_setting(ACCOUNT_BACKFILL_VERSION_KEY) == ACCOUNT_BACKFILL_VERSION:
        return state
    start = max(0, now_timestamp - ACCOUNT_BACKFILL_DAYS * _ACCOUNT_BACKFILL_DAY_SECONDS)
    end = max(0, now_timestamp - SYNC_WINDOW_SECONDS)
    for account_id in account_ids:
        if start >= end:
            continue
        state[str(account_id)] = {
            "start": start,
            "cursor": start,
            "end": end,
            "window_days": ACCOUNT_BACKFILL_WINDOW_DAYS,
            "retry_after": 0,
        }
    _save_account_backfill_state(state)
    set_setting(ACCOUNT_BACKFILL_VERSION_KEY, ACCOUNT_BACKFILL_VERSION)
    return state


def _coverage_time(timestamp: int | None = None):
    value = datetime.fromtimestamp(timestamp, timezone.utc) if timestamp is not None else _utc_now()
    return value.replace(tzinfo=None)


def _coverage_attempt(account_ids, start_ts: int, end_ts: int, source: str):
    if not account_ids:
        return
    now = _coverage_time()
    db = SessionLocal()
    try:
        for account_id in set(map(str, account_ids)):
            row = db.query(AccountDataCoverage).filter_by(
                account_id=account_id,
                start_ts=int(start_ts),
                end_ts=int(end_ts),
            ).first()
            if row is None:
                row = AccountDataCoverage(
                    account_id=account_id,
                    start_ts=int(start_ts),
                    end_ts=int(end_ts),
                    status="pending",
                    source=source,
                    attempts=0,
                )
                db.add(row)
            row.status = "pending"
            row.source = source
            row.attempts = int(row.attempts or 0) + 1
            row.error_code = None
            row.last_attempt_at = now
            row.retry_after = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _coverage_result(account_ids, start_ts: int, end_ts: int, *, returned_ids=None, error_codes=None, source="backfill"):
    if not account_ids:
        return
    returned = set(map(str, returned_ids or []))
    errors = {str(key): str(value) for key, value in (error_codes or {}).items()}
    now = _coverage_time()
    db = SessionLocal()
    try:
        for account_id in set(map(str, account_ids)):
            row = db.query(AccountDataCoverage).filter_by(
                account_id=account_id,
                start_ts=int(start_ts),
                end_ts=int(end_ts),
            ).first()
            if row is None:
                row = AccountDataCoverage(
                    account_id=account_id,
                    start_ts=int(start_ts),
                    end_ts=int(end_ts),
                    status="pending",
                    source=source,
                    attempts=1,
                    last_attempt_at=now,
                )
                db.add(row)
            code = errors.get(account_id)
            if code:
                row.status = "incomplete" if code == "act.missingdata" else "failed"
                row.error_code = code[:40]
                row.retry_after = _next_utc_reset().replace(tzinfo=None) if code == "act.missingdata" else None
            elif account_id in returned:
                row.status = "complete"
                row.error_code = None
                row.last_success_at = now
                row.retry_after = None
            else:
                row.status = "incomplete"
                row.error_code = "account_not_returned"
                row.retry_after = _next_utc_reset().replace(tzinfo=None)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _response_account_ids(payload: dict) -> set[str]:
    ids = set()
    for account in payload.get("accounts", []) if isinstance(payload, dict) else []:
        account_id = account.get("id") or account.get("account") if isinstance(account, dict) else None
        if account_id is not None:
            ids.add(str(account_id))
    return ids


def _coverage_errors(payload: dict, requested_account_ids) -> dict[str, str]:
    requested = set(map(str, requested_account_ids or []))
    errors = {}
    global_error = None
    raw_errors = payload.get("errlist") or payload.get("errors") or [] if isinstance(payload, dict) else []
    for item in raw_errors:
        if isinstance(item, dict):
            code = str(item.get("code") or "provider_error")
            account_id = item.get("account_id")
            if account_id is not None:
                errors[str(account_id)] = code
            elif code.startswith(("act.", "con.", "gen.")):
                global_error = code
        elif item:
            legacy = str(item).casefold()
            if "act.missingdata" in legacy:
                global_error = "act.missingdata"
            elif "act.failed" in legacy:
                global_error = "act.failed"
            else:
                global_error = "provider_warning"
    if global_error:
        for account_id in requested:
            errors.setdefault(account_id, global_error)
    returned = _response_account_ids(payload)
    for account_id in requested - returned:
        errors.setdefault(account_id, "account_not_returned")
    return errors


def _fetch_tracked_payload(access_url: str, start_ts: int, end_ts: int, account_ids, source: str):
    requested_ids = set(map(str, account_ids or []))
    attempt_recorded = False

    def record_attempt():
        nonlocal attempt_recorded
        tracked_ids = requested_ids
        if tracked_ids:
            _coverage_attempt(tracked_ids, start_ts, end_ts, source)
        attempt_recorded = True

    try:
        payload = _fetch_simplefin_payload(
            access_url,
            start_ts=start_ts,
            end_ts=end_ts,
            on_reserved=record_attempt,
        )
    except SimpleFINDailyLimitError:
        raise
    except Exception:
        if attempt_recorded and requested_ids:
            _coverage_result(
                requested_ids,
                start_ts,
                end_ts,
                returned_ids=(),
                error_codes={account_id: "request_failed" for account_id in requested_ids},
                source=source,
            )
        raise

    returned_ids = _response_account_ids(payload)
    tracked_ids = requested_ids | returned_ids
    if tracked_ids:
        if returned_ids - requested_ids:
            _coverage_attempt(returned_ids - requested_ids, start_ts, end_ts, source)
        error_codes = _coverage_errors(payload, tracked_ids)
    else:
        error_codes = _coverage_errors(payload, tracked_ids)
    return payload, error_codes, tracked_ids, returned_ids


def _merged_intervals(intervals, clip_start: int, clip_end: int):
    normalized = sorted(
        (max(clip_start, int(start)), min(clip_end, int(end)))
        for start, end in intervals
        if int(end) > clip_start and int(start) < clip_end
    )
    merged = []
    for start, end in normalized:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def get_account_data_history():
    """Return verified range coverage and stale-response status per account."""
    now_ts = int(_utc_now().timestamp())
    accounts = get_stored_accounts()
    db = SessionLocal()
    try:
        rows = db.query(AccountDataCoverage).all()
        by_account = {}
        for row in rows:
            by_account.setdefault(str(row.account_id), []).append(row)
    finally:
        db.close()

    queued = _load_account_backfill_state()
    result = []
    total_target_days = 0
    total_covered_days = 0
    for account in accounts:
        account_id = str(account.get("id"))
        account_rows = by_account.get(account_id, [])
        observed_ends = [row.end_ts for row in account_rows]
        account_target_end = min(now_ts, max(observed_ends, default=now_ts))
        account_target_start = max(0, account_target_end - ACCOUNT_BACKFILL_DAYS * _ACCOUNT_BACKFILL_DAY_SECONDS)
        successful = [row for row in account_rows if row.status == "complete"]
        merged = _merged_intervals(
            [(row.start_ts, row.end_ts) for row in successful],
            account_target_start,
            account_target_end,
        )
        covered_seconds = sum(end - start for start, end in merged)
        covered_days = min(ACCOUNT_BACKFILL_DAYS, covered_seconds / _ACCOUNT_BACKFILL_DAY_SECONDS)
        target_days = max(1, (account_target_end - account_target_start + _ACCOUNT_BACKFILL_DAY_SECONDS - 1) // _ACCOUNT_BACKFILL_DAY_SECONDS)
        total_target_days += target_days
        total_covered_days += covered_days

        gaps = []
        cursor = account_target_start
        for start, end in merged:
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < account_target_end:
            gaps.append((cursor, account_target_end))

        pending_progress = queued.get(account_id)
        account_gap_rows = [row for row in account_rows if row.status != "complete"]
        reported_gaps = []
        for start, end in gaps:
            overlapping = [row for row in account_gap_rows if row.start_ts < end and row.end_ts > start]
            latest_problem = max(overlapping, key=lambda row: row.last_attempt_at or datetime.min, default=None)
            queued_here = bool(
                pending_progress
                and pending_progress.get("cursor", pending_progress.get("end", 0)) < pending_progress.get("end", 0)
                and pending_progress.get("cursor", 0) < end
                and pending_progress.get("end", 0) > start
            )
            start_date = datetime.fromtimestamp(start, timezone.utc).date().isoformat()
            end_date = datetime.fromtimestamp(end, timezone.utc).date().isoformat()
            reported_gaps.append({
                "start_ts": start,
                "end_ts": end,
                "start_date": start_date,
                "end_date": end_date,
                "days": max(1, (end - start + _ACCOUNT_BACKFILL_DAY_SECONDS - 1) // _ACCOUNT_BACKFILL_DAY_SECONDS),
                "status": "pending" if queued_here else (latest_problem.status if latest_problem else "missing"),
                "error_code": latest_problem.error_code if latest_problem else None,
                "attempts": int(latest_problem.attempts or 0) if latest_problem else 0,
            })

        last_received = max((row.last_success_at for row in successful if row.last_success_at), default=None)
        age_seconds = (_utc_now().replace(tzinfo=None) - last_received).total_seconds() if last_received else None
        stale = age_seconds is None or age_seconds > 48 * 60 * 60
        latest_problem = max(
            account_rows,
            key=lambda row: row.last_attempt_at or datetime.min,
            default=None,
        )
        result.append({
            "account_id": account_id,
            "name": account.get("nickname") or account.get("name") or "Account",
            "institution": account.get("org") or "",
            "currency": account.get("currency") or "USD",
            "coverage_days": round(covered_days, 1),
            "target_days": target_days,
            "coverage_as_of": datetime.fromtimestamp(account_target_end, timezone.utc).isoformat(),
            "coverage_percent": round(min(100.0, covered_days * 100 / target_days), 1),
            "gaps": reported_gaps,
            "last_received_at": last_received.isoformat() + "Z" if last_received else None,
            "hours_since_last_received": round(age_seconds / 3600, 1) if age_seconds is not None else None,
            "stale": stale,
            "latest_error_code": latest_problem.error_code if latest_problem and latest_problem.status != "complete" else None,
        })

    return {
        "target_days": ACCOUNT_BACKFILL_DAYS,
        "as_of": _utc_now().isoformat(),
        "accounts": result,
        "overall_coverage_percent": round(total_covered_days * 100 / total_target_days, 1) if total_target_days else 100.0,
        "covered_days": round(total_covered_days, 1),
        "target_days_total": total_target_days,
        "verified_means_provider_returned_account_without_a_data_completeness_error": True,
    }


def queue_account_history_retry(account_id: str, start_ts: int, end_ts: int) -> dict:
    if not get_simplefin_token():
        raise ValueError("Connect SimpleFIN before retrying account history.")
    if _read_sync_progress().get("state") == "running":
        raise ValueError("A SimpleFIN sync is already running. Wait for it to finish before queuing another range.")
    account_id = str(account_id)
    if not any(str(account.get("id")) == account_id for account in get_stored_accounts()):
        raise ValueError("Account not found.")
    start_ts, end_ts = int(start_ts), int(end_ts)
    now_ts = int(_utc_now().timestamp())
    if start_ts < 0 or end_ts > now_ts or end_ts <= start_ts:
        raise ValueError("Choose a valid missing date range.")
    history = get_account_data_history()
    account = next((item for item in history["accounts"] if item["account_id"] == account_id), None)
    if not account or not any(
        gap["start_ts"] <= start_ts and end_ts <= gap["end_ts"]
        for gap in account["gaps"]
    ):
        raise ValueError("That date range is not currently recorded as a coverage gap.")

    state = _load_account_backfill_state()
    existing = state.get(account_id)
    if existing and existing.get("cursor", 0) < existing.get("end", 0):
        raise ValueError("A backfill for this account is already queued.")
    state[account_id] = {
        "start": start_ts,
        "cursor": start_ts,
        "end": end_ts,
        "window_days": ACCOUNT_BACKFILL_WINDOW_DAYS,
        "retry_after": 0,
    }
    _save_account_backfill_state(state)
    return {"status": "queued", "account_id": account_id, "start_ts": start_ts, "end_ts": end_ts}


def _set_sync_progress(sync_id: str, state: str, phase: str, message: str, warning: str | None = None):
    history = get_account_data_history()
    token = get_simplefin_token()
    budget = _request_budget_status(token)
    return _write_sync_progress(
        sync_id=sync_id,
        state=state,
        active=state == "running",
        phase=phase,
        message=message,
        warning=warning,
        coverage_percent=history["overall_coverage_percent"],
        covered_days=history["covered_days"],
        target_days=history["target_days_total"],
        accounts_count=len(history["accounts"]),
        requests_today=budget["requests_today"],
        daily_request_limit=budget["daily_limit"],
        requests_remaining=budget["requests_remaining"],
        utc_day_resets_at=budget["utc_day_resets_at"],
    )


def fetch_and_sync_simplefin():
    """Fetch normalized financial data from SimpleFIN and persist to DB."""
    access_url = get_simplefin_token()

    if access_url and access_url != "":
        sync_id = uuid4().hex
        phase = "preparing"
        try:
            now_timestamp = int(datetime.now(timezone.utc).timestamp())
            backfill_limit = max(0, now_timestamp - SYNC_WINDOW_SECONDS)
            cursor = _get_sync_cursor(now_timestamp)
            simplefin_errors = []
            _set_sync_progress(sync_id, "running", "preparing", "Preparing the recent sync and 12-month account history.")
            rules = _load_category_rules()
            known_account_ids = {
                str(account.get("id"))
                for account in get_stored_accounts()
                if account.get("id") is not None
            }
            account_backfill_state = _load_account_backfill_state()
            account_backfill_state = _queue_initial_account_history(
                known_account_ids,
                now_timestamp,
                account_backfill_state,
            )
            previous_latest_timestamp = _stored_latest_transaction_timestamp()
            previous_latest_date = (
                datetime.fromtimestamp(previous_latest_timestamp, timezone.utc).date().isoformat()
                if previous_latest_timestamp is not None
                else None
            )

            incoming_transactions_by_id = {}
            backfill_windows = 0

            def note_coverage_errors(error_codes):
                for code in sorted(set(error_codes.values())):
                    if code == "act.missingdata":
                        message = "SimpleFIN reported an incomplete transaction listing; the affected range will be narrowed and retried."
                    elif code == "account_not_returned":
                        message = "SimpleFIN did not return data for one or more linked accounts; those ranges remain unverified."
                    else:
                        message = "SimpleFIN reported an account or connection error; the affected ranges remain unverified."
                    if message not in simplefin_errors:
                        simplefin_errors.append(message)

            def update_progress(next_phase, message, *, state="running", warning=None):
                nonlocal phase
                phase = next_phase
                _set_sync_progress(sync_id, state, phase, message, warning)

            def save_normalized_payload(normalized):
                discovered_new_account = False
                account_backfill_end = max(0, now_timestamp - SYNC_WINDOW_SECONDS)
                account_backfill_start = max(0, now_timestamp - ACCOUNT_BACKFILL_DAYS * _ACCOUNT_BACKFILL_DAY_SECONDS)
                for account in normalized.get("accounts", []):
                    account_id = str(account.get("id"))
                    if account_id in known_account_ids:
                        continue
                    known_account_ids.add(account_id)
                    if account_id not in account_backfill_state and account_backfill_start < account_backfill_end:
                        account_backfill_state[account_id] = {
                            "start": account_backfill_start,
                            "cursor": account_backfill_start,
                            "end": account_backfill_end,
                            "window_days": ACCOUNT_BACKFILL_WINDOW_DAYS,
                            "retry_after": 0,
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
                update_progress("recent_history", "Catching up recent transaction history.")
                payload, window_errors, coverage_ids, returned_ids = _fetch_tracked_payload(
                    access_url,
                    cursor,
                    window_end,
                    known_account_ids,
                    "routine_backfill",
                )
                for key in ("errlist", "errors"):
                    if payload.get(key):
                        note_coverage_errors(window_errors)

                normalized = _normalize_simplefin_payload(payload, rules)
                save_normalized_payload(normalized)
                _coverage_result(coverage_ids, cursor, window_end, returned_ids=returned_ids, error_codes=window_errors, source="routine_backfill")

                if window_errors:
                    note_coverage_errors(window_errors)
                    break

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
            update_progress("recent", "Checking the latest day of transactions.")
            payload, rolling_errors, coverage_ids, returned_ids = _fetch_tracked_payload(
                access_url,
                rolling_start,
                now_timestamp,
                known_account_ids,
                "routine",
            )
            for key in ("errlist", "errors"):
                if payload.get(key):
                    note_coverage_errors(rolling_errors)
            normalized = _normalize_simplefin_payload(payload, rules)
            save_normalized_payload(normalized)
            _coverage_result(coverage_ids, rolling_start, now_timestamp, returned_ids=returned_ids, error_codes=rolling_errors, source="routine")
            note_coverage_errors(rolling_errors)

            if not backfill_pending and not rolling_errors:
                cursor = now_timestamp
                set_setting(SYNC_CURSOR_SETTING_KEY, str(cursor))

            account_backfill_windows = 0
            while account_backfill_state:
                batch_windows = 0
                while account_backfill_state and batch_windows < ACCOUNT_BACKFILL_BATCH_WINDOWS:
                    eligible = [
                        key for key, progress in account_backfill_state.items()
                        if int(progress.get("retry_after", 0) or 0) <= int(_utc_now().timestamp())
                    ]
                    if not eligible:
                        break
                    account_id = min(
                        eligible,
                        key=lambda key: (
                            account_backfill_state[key]["cursor"],
                            account_backfill_state[key]["end"],
                            account_backfill_state[key]["window_days"],
                            key,
                        ),
                    )
                    cursor = account_backfill_state[account_id]["cursor"]
                    window_days = account_backfill_state[account_id]["window_days"]
                    window_end = min(
                        cursor + window_days * _ACCOUNT_BACKFILL_DAY_SECONDS,
                        account_backfill_state[account_id]["end"],
                    )
                    group = [
                        key for key, progress in account_backfill_state.items()
                        if progress["cursor"] == cursor
                        and progress["end"] == account_backfill_state[account_id]["end"]
                        and progress["window_days"] == window_days
                        and int(progress.get("retry_after", 0) or 0) <= int(_utc_now().timestamp())
                    ]

                    update_progress("account_backfill", "Verifying account history ranges.")
                    payload, window_errors, coverage_ids, returned_ids = _fetch_tracked_payload(
                        access_url,
                        cursor,
                        window_end,
                        known_account_ids,
                        "account_backfill",
                    )
                    for key in ("errlist", "errors"):
                        if payload.get(key):
                            note_coverage_errors(window_errors)
                    normalized = _normalize_simplefin_payload(payload, rules)
                    save_normalized_payload(normalized)
                    _coverage_result(coverage_ids, cursor, window_end, returned_ids=returned_ids, error_codes=window_errors, source="account_backfill")
                    note_coverage_errors(window_errors)

                    for queued_account_id in group:
                        progress = account_backfill_state.get(queued_account_id)
                        if not progress:
                            continue
                        error_code = window_errors.get(queued_account_id)
                        if error_code == "act.missingdata":
                            if window_days > 1:
                                progress["window_days"] = max(1, window_days // 2)
                                progress["retry_after"] = 0
                            else:
                                progress["retry_after"] = int(_next_utc_reset().timestamp())
                                message = "A one-day history range is still incomplete; it will remain flagged and retry after the daily reset."
                                if message not in simplefin_errors:
                                    simplefin_errors.append(message)
                        elif error_code:
                            progress["retry_after"] = int(_next_utc_reset().timestamp())
                        else:
                            progress["cursor"] = window_end
                            progress["window_days"] = ACCOUNT_BACKFILL_WINDOW_DAYS
                            progress["retry_after"] = 0
                            if window_end >= progress["end"]:
                                account_backfill_state.pop(queued_account_id, None)
                    _save_account_backfill_state(account_backfill_state)
                    account_backfill_windows += 1
                    batch_windows += 1
                    update_progress(
                        "account_backfill",
                        "Verifying account history ranges.",
                        warning=simplefin_errors[-1] if simplefin_errors else None,
                    )

                if not any(
                    int(progress.get("retry_after", 0) or 0) <= int(_utc_now().timestamp())
                    for progress in account_backfill_state.values()
                ):
                    break

            account_backfill_pending = bool(account_backfill_state)
            account_backfill_remaining_days = sum(
                (progress["end"] - progress["cursor"] + _ACCOUNT_BACKFILL_DAY_SECONDS - 1) // _ACCOUNT_BACKFILL_DAY_SECONDS
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
                f"SimpleFIN checked {total_windows} date window(s); backfill requests are at most "
                f"{ACCOUNT_BACKFILL_WINDOW_DAYS} days and are throttled to the daily request budget."
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
                    f" Account history verified {account_backfill_windows} additional window(s)."
                )
            if account_backfill_pending:
                message += (
                    f" Account history is incomplete: about {account_backfill_remaining_days} day(s) "
                    "remain and will resume on the next sync."
                )
            pending_sync = backfill_pending or account_backfill_pending or bool(rolling_errors)
            if pending_sync:
                message += " Some ranges remain unverified; see Settings → Data History for their status."
            budget = _request_budget_status(access_url)
            message += f" SimpleFIN requests today: {budget['requests_today']}/{budget['daily_limit']}."

            _record_sync_state("pending" if pending_sync else "success", message)
            _set_sync_progress(
                sync_id,
                "paused" if pending_sync else "complete",
                phase if pending_sync else "complete",
                message,
                simplefin_errors[-1] if simplefin_errors else None,
            )
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
                "sync_progress": _read_sync_progress(),
            }
        except SimpleFINDailyLimitError as exc:
            message = str(exc)
            _record_sync_state("pending", message)
            _set_sync_progress(
                sync_id,
                "paused",
                phase,
                message,
                "The saved backfill cursor will resume after the UTC daily quota reset.",
            )
            return {
                "status": "in_progress",
                "message": message,
                "rate_limited": True,
                "requests_today": exc.requests_today,
                "daily_request_limit": SIMPLEFIN_MAX_REQUESTS_PER_UTC_DAY,
                "resume_at": exc.reset_at,
                "sync_progress": _read_sync_progress(),
            }
        except Exception as e:
            logger.error(f"Error fetching SimpleFIN data: {e}")
            message = "Sync failed. Any verified progress is saved and can be retried from Data History."
            saved_backfills = locals().get("account_backfill_state", {})
            if saved_backfills:
                message += (
                    f" Backfill progress is saved for {len(saved_backfills)} account(s) and can resume."
                )
            _record_sync_state("error", message)
            _set_sync_progress(sync_id, "paused", phase, message, "A sync error interrupted at least one date range.")
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
