"""Read-only Home Assistant sensor data for the Finance Dashboard."""
from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from database import get_stored_transactions
from models import (
    ASSET_ACCOUNT_TYPES,
    LIABILITY_ACCOUNT_TYPES,
    Account,
    ManualAsset,
    SessionLocal,
    Transaction,
)

OPTIONS_PATH = "/data/options.json"
METRICS_TOKEN_OPTION = "ha_metrics_api_token"
MAX_EXPOSED_TRANSACTIONS = 20
ZERO = Decimal("0")
TRANSFER_KEYWORDS = (
    "transfer",
    "zelle",
    "venmo",
    "cash app",
    "cashapp",
    "payment to self",
    "ach xfer",
    "external transfer",
    "internal transfer",
)


def read_metrics_api_token(options_path: str = OPTIONS_PATH) -> str:
    """Read the optional metrics token without logging or returning other options."""
    try:
        with open(options_path, encoding="utf-8") as options_file:
            options = json.load(options_file)
    except FileNotFoundError:
        return ""

    if not isinstance(options, dict):
        raise ValueError("Add-on options must be a JSON object.")
    token = options.get(METRICS_TOKEN_OPTION, "")
    if token is None:
        return ""
    if not isinstance(token, str):
        raise ValueError("The metrics API token must be a string.")
    return token.strip()


def metrics_token_matches(configured_token: str, presented_token: str | None) -> bool:
    """Accept requests without a token only when token authentication is disabled."""
    if not configured_token:
        return True
    return bool(presented_token) and hmac.compare_digest(configured_token, presented_token)


def _decimal(value) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else "0"))
        return amount if amount.is_finite() else ZERO
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def _number(value: Decimal) -> float:
    return float(value)


def _currency(value: str | None) -> str:
    return str(value or "USD").strip().upper() or "USD"


def _is_dashboard_transfer_like(
    payee: str,
    description: str,
    category: str | None,
) -> bool:
    """Match the dashboard's explicit-category override and transfer keywords."""
    normalized_category = str(category or "").strip().lower()
    if normalized_category and normalized_category != "uncategorized":
        return bool(re.search(r"\btransfers?\b", normalized_category))
    haystack = f"{payee or ''} {description or ''}".lower()
    return any(keyword in haystack for keyword in TRANSFER_KEYWORDS)


def build_ha_metrics(
    status: dict,
    *,
    include_transactions: bool = False,
    now: datetime | None = None,
) -> dict:
    """Build bounded metric data using the same balance and cash-flow rules as the UI."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    month_start = current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_timestamp = int(month_start.timestamp())

    db = SessionLocal()
    try:
        accounts = db.query(Account).all()
        account_by_id = {str(account.id): account for account in accounts}
        account_metrics = []
        net_worth_by_currency: dict[str, dict[str, Decimal | int]] = {}

        def net_worth_group(currency: str) -> dict[str, Decimal | int]:
            return net_worth_by_currency.setdefault(
                currency,
                {"assets": ZERO, "liabilities": ZERO, "classified_count": 0},
            )

        for account in accounts:
            currency = _currency(account.currency)
            balance = _decimal(account.balance)
            account_metrics.append(
                {
                    "id": str(account.id),
                    "name": account.name or "Account",
                    "nickname": account.nickname or "",
                    "currency": currency,
                    "balance": _number(balance),
                    "account_type": account.account_type or "unclassified",
                    "updated_at": (
                        account.updated_at.isoformat() if account.updated_at else None
                    ),
                }
            )

            if account.account_type in ASSET_ACCOUNT_TYPES:
                group = net_worth_group(currency)
                group["assets"] += balance
                group["classified_count"] += 1
            elif account.account_type in LIABILITY_ACCOUNT_TYPES:
                group = net_worth_group(currency)
                group["liabilities"] += abs(balance)
                group["classified_count"] += 1

        for asset in db.query(ManualAsset).all():
            currency = _currency(asset.currency)
            group = net_worth_group(currency)
            group["assets"] += _decimal(asset.value)
            group["classified_count"] += 1

        cash_flow_by_currency: dict[str, dict[str, Decimal | int]] = {}
        for account in accounts:
            cash_flow_by_currency.setdefault(
                _currency(account.currency),
                {"income": ZERO, "spending": ZERO, "transaction_count": 0},
            )
        month_transactions = (
            db.query(Transaction)
            .filter(Transaction.date >= month_start_timestamp)
            .all()
        )
        for transaction in month_transactions:
            if transaction.pending:
                continue
            if _is_dashboard_transfer_like(
                transaction.payee,
                transaction.description,
                transaction.category,
            ):
                continue

            amount = _decimal(transaction.amount)
            if amount == ZERO:
                continue
            account = account_by_id.get(str(transaction.account_id))
            currency = _currency(account.currency if account else "USD")
            group = cash_flow_by_currency.setdefault(
                currency,
                {"income": ZERO, "spending": ZERO, "transaction_count": 0},
            )
            if amount > ZERO:
                group["income"] += amount
            else:
                group["spending"] += abs(amount)
            group["transaction_count"] += 1

        net_worth = []
        for currency, totals in sorted(net_worth_by_currency.items()):
            if not totals["classified_count"]:
                continue
            assets = totals["assets"]
            liabilities = totals["liabilities"]
            net_worth.append(
                {
                    "currency": currency,
                    "assets": _number(assets),
                    "liabilities": _number(liabilities),
                    "net_worth": _number(assets - liabilities),
                }
            )

        cash_flow = []
        for currency, totals in sorted(cash_flow_by_currency.items()):
            income = totals["income"]
            spending = totals["spending"]
            cash_flow.append(
                {
                    "currency": currency,
                    "income": _number(income),
                    "spending": _number(spending),
                    "net": _number(income - spending),
                    "transaction_count": totals["transaction_count"],
                }
            )

        result = {
            "month": month_start.strftime("%Y-%m"),
            "status": {
                "connected": bool(status.get("connected", False)),
                "last_sync_status": str(status.get("last_sync_status") or "never"),
                "last_sync_at": status.get("last_sync_at"),
                "incoming_transaction_count": int(
                    status.get("incoming_transaction_count") or 0
                ),
                "latest_incoming_transaction_date": status.get(
                    "latest_incoming_transaction_date"
                ),
                "newest_transaction_date_advanced": status.get(
                    "newest_transaction_date_advanced"
                ),
                "account_count": int(status.get("account_count") or 0),
                "transaction_count": int(status.get("transaction_count") or 0),
            },
            "accounts": sorted(
                account_metrics,
                key=lambda item: (item["name"].casefold(), item["id"]),
            ),
            "net_worth": net_worth,
            "cash_flow": cash_flow,
        }
    finally:
        db.close()

    if include_transactions:
        result["transactions"] = get_stored_transactions(limit=MAX_EXPOSED_TRANSACTIONS, offset=0)
    return result
