"""Privacy-minimized AI chat for financial goal planning."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from ai_assistant import _provider_key, get_ai_settings
from database import get_manual_assets, get_stored_accounts
from goals import get_goals
from models import LIABILITY_ACCOUNT_TYPES, SessionLocal, Transaction
from recurring_analysis import _is_transfer_like

MAX_CHAT_MESSAGE_LENGTH = 2000
MAX_CHAT_HISTORY_MESSAGE_LENGTH = 4000
MAX_CHAT_HISTORY = 20
CHAT_SYSTEM_PROMPT = """You help the user think through financial goals using a minimized financial snapshot.
The snapshot contains aggregate balances grouped by currency and account type, monthly income and category spending totals, and unnamed goal summaries. It contains no account names, account IDs, merchant names, or transaction descriptions. Never ask for account names, account numbers, or other identifying details.

Use only the information provided. Keep currencies separate; never convert currencies or invent balances. Treat projections as estimates, not guarantees or professional financial advice. Do not assume an APR, return, inflation rate, or contribution amount unless the user or snapshot provides it. Ask a concise follow-up if important inputs are missing.

When you have enough information to suggest one concrete goal, include exactly one final structured block in this format, with no markdown fence:
<goal_draft>{"goal_type":"savings|debt_payoff|retirement","name":"short non-identifying label","target_amount":"number","target_date":"YYYY-MM-DD","apr_percent":"number","planned_monthly":"number","retirement_timing_mode":"year|age","retirement_year":0,"birth_year":0,"target_retirement_age":0,"expected_return_percent":"number","inflation_percent":"number"}</goal_draft>
Include only relevant fields. Never include account IDs or account names in a draft. The app will require the user to choose sources and review/save the draft."""


class AIChatError(ValueError):
    pass


def _value(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _decimal(value) -> Decimal:
    try:
        amount = Decimal(str(value or "0"))
        return amount if amount.is_finite() else Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _number(value: Decimal) -> float:
    return round(float(value), 2)


def _category_rows(values, *, limit: int = 20) -> list[dict]:
    ordered = sorted(values.items(), key=lambda item: (-item[1]["amount"], item[0]))
    rows = [
        {"category": category, "amount": _number(item["amount"]), "count": item["count"]}
        for category, item in ordered[:limit]
    ]
    remainder = ordered[limit:]
    if remainder:
        rows.append({
            "category": "Other categories (combined)",
            "amount": _number(sum((item["amount"] for _, item in remainder), Decimal("0"))),
            "count": sum(item["count"] for _, item in remainder),
        })
    return rows


def _cutoff_date(today: date) -> date:
    try:
        return today.replace(year=today.year - 1)
    except ValueError:
        return today.replace(year=today.year - 1, day=28)


def _recent_transactions(today: date):
    cutoff = int(datetime.combine(_cutoff_date(today), datetime.min.time(), timezone.utc).timestamp())
    db = SessionLocal()
    try:
        return db.query(Transaction).filter(
            Transaction.date >= cutoff,
            Transaction.pending.is_(False),
        ).order_by(Transaction.date.asc()).all()
    finally:
        db.close()


def build_sanitized_financial_context(accounts, manual_assets, goals, transactions, *, today: date | None = None) -> dict:
    """Build the only financial payload permitted in a chat-completions request."""
    today = today or date.today()
    account_totals = defaultdict(lambda: {"account_count": 0, "balance_total": Decimal("0")})
    account_currencies = {}
    for account in accounts:
        account_id = str(_value(account, "id", ""))
        currency = str(_value(account, "currency", "USD") or "USD").upper()
        account_type = str(_value(account, "account_type", "unclassified") or "unclassified")
        balance = _decimal(_value(account, "balance", "0"))
        if account_type in LIABILITY_ACCOUNT_TYPES:
            balance = abs(balance)
        group = account_totals[(currency, account_type)]
        group["account_count"] += 1
        group["balance_total"] += balance
        if account_id:
            account_currencies[account_id] = currency

    asset_totals = defaultdict(lambda: {"asset_count": 0, "value_total": Decimal("0")})
    for asset in manual_assets:
        currency = str(_value(asset, "currency", "USD") or "USD").upper()
        group = asset_totals[currency]
        group["asset_count"] += 1
        group["value_total"] += _decimal(_value(asset, "value", "0"))

    period_totals = defaultdict(lambda: {
        "income_total": Decimal("0"),
        "income_by_category": defaultdict(lambda: {"amount": Decimal("0"), "count": 0}),
        "spending_total": Decimal("0"),
        "spending_by_category": defaultdict(lambda: {"amount": Decimal("0"), "count": 0}),
    })
    cutoff = _cutoff_date(today)
    for transaction in transactions:
        timestamp = _value(transaction, "date")
        if not timestamp:
            continue
        try:
            transaction_date = datetime.fromtimestamp(int(timestamp), timezone.utc).date()
        except (OSError, OverflowError, TypeError, ValueError):
            continue
        if transaction_date < cutoff or transaction_date > today:
            continue
        category = str(_value(transaction, "category", "Uncategorized") or "Uncategorized").strip() or "Uncategorized"
        screening_text = f"{_value(transaction, 'payee', '') or ''} {_value(transaction, 'description', '') or ''}".strip()
        if _is_transfer_like(screening_text, category):
            continue
        currency = account_currencies.get(str(_value(transaction, "account_id", "")))
        if not currency:
            continue
        amount = _decimal(_value(transaction, "amount", "0"))
        if amount == 0:
            continue
        month = transaction_date.strftime("%Y-%m")
        group = period_totals[(month, currency)]
        if amount > 0:
            group["income_total"] += amount
            category_total = group["income_by_category"][category]
            category_total["amount"] += amount
            category_total["count"] += 1
        else:
            spending = abs(amount)
            group["spending_total"] += spending
            category_total = group["spending_by_category"][category]
            category_total["amount"] += spending
            category_total["count"] += 1

    monthly_activity = []
    for (month, currency), group in sorted(period_totals.items()):
        monthly_activity.append({
            "month": month,
            "currency": currency,
            "income_total": _number(group["income_total"]),
            "income_by_category": _category_rows(group["income_by_category"]),
            "spending_total": _number(group["spending_total"]),
            "spending_by_category": _category_rows(group["spending_by_category"]),
        })

    goal_summaries = []
    for goal in goals:
        safe_goal = {
            "goal_type": goal.get("goal_type"),
            "currency": goal.get("currency"),
            "current_amount": goal.get("current_amount"),
            "target_amount": goal.get("target_amount"),
            "remaining_amount": goal.get("remaining_amount"),
            "target_date": goal.get("target_date"),
            "retirement_year": goal.get("retirement_year"),
            "required_monthly": goal.get("required_monthly"),
            "planned_monthly": goal.get("planned_monthly"),
            "status": goal.get("status"),
        }
        if goal.get("goal_type") == "debt_payoff":
            safe_goal["apr_percent"] = goal.get("apr_percent")
        if goal.get("goal_type") == "retirement":
            safe_goal["expected_return_percent"] = goal.get("expected_return_percent")
            safe_goal["inflation_percent"] = goal.get("inflation_percent")
            safe_goal["target_nominal_amount"] = goal.get("target_nominal_amount")
        goal_summaries.append(safe_goal)

    return {
        "as_of_month": today.strftime("%Y-%m"),
        "history_window_months": 12,
        "balances_by_currency_and_account_type": [
            {
                "currency": currency,
                "account_type": account_type,
                "account_count": values["account_count"],
                "balance_total": _number(values["balance_total"]),
            }
            for (currency, account_type), values in sorted(account_totals.items())
        ],
        "manually_tracked_assets_by_currency": [
            {
                "currency": currency,
                "asset_count": values["asset_count"],
                "value_total": _number(values["value_total"]),
            }
            for currency, values in sorted(asset_totals.items())
        ],
        "monthly_activity": monthly_activity,
        "existing_goals": goal_summaries,
    }


def _sensitive_terms(accounts, manual_assets, goals) -> list[str]:
    terms = set()
    for account in accounts:
        for key in ("id", "name", "nickname", "org"):
            value = str(_value(account, key, "") or "").strip()
            if len(value) >= 3:
                terms.add(value)
    for asset in manual_assets:
        for key in ("id", "name"):
            value = str(_value(asset, key, "") or "").strip()
            if len(value) >= 3:
                terms.add(value)
    for goal in goals:
        for value in [goal.get("id"), goal.get("name"), *goal.get("source_names", [])]:
            value = str(value or "").strip()
            if len(value) >= 3:
                terms.add(value)
        for value in [*goal.get("account_ids", []), *goal.get("manual_asset_ids", [])]:
            value = str(value or "").strip()
            if len(value) >= 3:
                terms.add(value)
    return sorted(terms, key=len, reverse=True)


def sanitize_message(message: str, sensitive_terms: list[str], *, max_length: int = MAX_CHAT_MESSAGE_LENGTH) -> tuple[str, int]:
    if not isinstance(message, str):
        raise AIChatError("Enter a message to continue.")
    if len(message) > max_length:
        raise AIChatError(f"Messages must be {max_length} characters or fewer.")
    sanitized = message
    redactions = 0
    for term in sensitive_terms:
        if len(term) < 3:
            continue
        escaped = re.escape(term)
        escaped = escaped.replace(r"\ ", r"\s+")
        pattern = re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)
        sanitized, replacements = pattern.subn("[redacted]", sanitized)
        redactions += replacements
    account_like_number = re.compile(r"(?<!\d)(?:\d[ -]?){7,}\d(?!\d)")

    def redact_number(match):
        nonlocal redactions
        value = match.group(0)
        before = sanitized[max(0, match.start() - 1):match.start()]
        after = sanitized[match.end():match.end() + 10]
        if before == "$" or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return value
        if re.match(r"\s*(?:usd|eur|gbp|cad|aud|dollars?)\b", after, re.IGNORECASE):
            return value
        redactions += 1
        return "[redacted number]"

    sanitized = account_like_number.sub(redact_number, sanitized)
    return sanitized, redactions


def _load_chat_data():
    accounts = get_stored_accounts()
    manual_assets = get_manual_assets()
    goals = get_goals(accounts, manual_assets)
    sensitive_terms = _sensitive_terms(accounts, manual_assets, goals)
    return accounts, manual_assets, goals, sensitive_terms


def preview_chat_message(message: str) -> dict:
    accounts, manual_assets, goals, sensitive_terms = _load_chat_data()
    sanitized, redaction_count = sanitize_message(message, sensitive_terms)
    return {
        "sanitized_message": sanitized,
        "redaction_count": redaction_count,
        "notice": "Unknown names may not be detected. Review this text before sending it to your selected AI provider.",
    }


def _validate_goal_draft(candidate, sensitive_terms):
    if not isinstance(candidate, dict):
        return None
    goal_type = str(candidate.get("goal_type") or "").strip()
    if goal_type not in {"debt_payoff", "savings", "retirement"}:
        return None
    name, _ = sanitize_message(str(candidate.get("name") or "Suggested goal"), sensitive_terms)
    draft = {"goal_type": goal_type, "name": name[:80] or "Suggested goal"}
    fields = (
        "target_amount", "target_date", "apr_percent", "planned_monthly",
        "retirement_timing_mode", "birth_year", "target_retirement_age",
        "retirement_year", "expected_return_percent", "inflation_percent",
    )
    for field in fields:
        value = candidate.get(field)
        if value is None or isinstance(value, (dict, list, bool)):
            continue
        text = str(value).strip()
        if not text or len(text) > 50:
            continue
        if field == "target_date":
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                try:
                    date.fromisoformat(text)
                except ValueError:
                    continue
                draft[field] = text
        elif field == "retirement_timing_mode":
            if text in {"age", "year"}:
                draft[field] = text
        elif field in {"birth_year", "target_retirement_age", "retirement_year"}:
            if re.fullmatch(r"\d{1,4}", text):
                draft[field] = int(text)
        else:
            try:
                number = Decimal(text)
            except InvalidOperation:
                continue
            if number.is_finite() and number >= 0:
                draft[field] = format(number, "f")
    return draft


def _extract_goal_draft(response_text: str, sensitive_terms: list[str]):
    match = re.search(r"<goal_draft>\s*([\s\S]*?)\s*</goal_draft>", response_text, re.IGNORECASE)
    if not match:
        return response_text.strip(), None, 0
    try:
        candidate = json.loads(match.group(1))
    except (TypeError, json.JSONDecodeError):
        return response_text.replace(match.group(0), "").strip(), None, 0
    draft = _validate_goal_draft(candidate, sensitive_terms)
    reply = response_text.replace(match.group(0), "").strip()
    if draft:
        draft["name"], redactions = sanitize_message(draft["name"], sensitive_terms)
        return reply, draft, redactions
    return reply, None, 0


def _completion(model: str, api_key: str, messages: list[dict]):
    from litellm import completion

    return completion(
        model=model,
        messages=messages,
        api_key=api_key,
        temperature=0.2,
        max_tokens=700,
    )


def create_goal_chat_response(messages: list[dict]) -> dict:
    if not isinstance(messages, list) or not messages or len(messages) > MAX_CHAT_HISTORY:
        raise AIChatError(f"Send between 1 and {MAX_CHAT_HISTORY} chat messages.")
    accounts, manual_assets, goals, sensitive_terms = _load_chat_data()
    safe_messages = []
    redaction_count = 0
    total_chars = 0
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            raise AIChatError("Chat history contains an unsupported message.")
        content = item.get("content")
        max_length = MAX_CHAT_MESSAGE_LENGTH if item.get("role") == "user" else MAX_CHAT_HISTORY_MESSAGE_LENGTH
        if not isinstance(content, str) or len(content) > max_length:
            raise AIChatError("A chat message is too long.")
        total_chars += len(content)
        sanitized, count = sanitize_message(content, sensitive_terms, max_length=max_length)
        redaction_count += count
        safe_messages.append({"role": item["role"], "content": sanitized})
    if total_chars > 16000:
        raise AIChatError("This conversation is too long. Start a new chat to continue.")
    if safe_messages[-1]["role"] != "user":
        raise AIChatError("Send a user message to continue the chat.")

    settings = get_ai_settings()
    if not settings["enabled"]:
        raise AIChatError("Enable AI assistance in AI settings before starting a chat.")
    provider = settings["provider"]
    api_key = _provider_key(provider)
    if not api_key:
        raise AIChatError(f"No API key is saved for the selected provider ({provider}).")

    context = build_sanitized_financial_context(
        accounts,
        manual_assets,
        goals,
        _recent_transactions(date.today()),
    )
    system_content = (
        CHAT_SYSTEM_PROMPT
        + "\n\nSanitized aggregate financial snapshot (JSON):\n"
        + json.dumps(context, separators=(",", ":"), ensure_ascii=False)
    )
    provider_messages = [{"role": "system", "content": system_content}, *safe_messages]
    try:
        response = _completion(settings["model"], api_key, provider_messages)
        response_text = response.choices[0].message.content or ""
        if not isinstance(response_text, str):
            response_text = str(response_text)
    except Exception as exc:
        raise AIChatError("The AI provider request failed. No chat content was written to app logs.") from exc

    reply, goal_draft, response_redactions = _extract_goal_draft(response_text, sensitive_terms)
    reply, output_redactions = sanitize_message(reply, sensitive_terms, max_length=MAX_CHAT_HISTORY_MESSAGE_LENGTH)
    return {
        "reply": reply,
        "goal_draft": goal_draft,
        "provider": provider,
        "model": settings["model"],
        "redaction_count": redaction_count + response_redactions + output_redactions,
        "user_message": safe_messages[-1]["content"],
    }
