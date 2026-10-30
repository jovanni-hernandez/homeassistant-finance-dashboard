"""Snowball-style payoff projections based on a dedicated funding account."""
from __future__ import annotations

import json
import logging
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from database import get_setting, set_setting
from models import LIABILITY_ACCOUNT_TYPES, SessionLocal, Transaction
from ai_assistant import _provider_key, get_ai_settings

SETTINGS_KEY = "debt_payoff_planner"
CENT = Decimal("0.01")
ZERO = Decimal("0")
MAX_PROJECTION_MONTHS = 1200
STRATEGIES = {"highest_balance", "lowest_balance", "highest_apr", "manual"}
logger = logging.getLogger("finance_dashboard")


class DebtPayoffValidationError(ValueError):
    pass


class DebtPayoffEstimateError(ValueError):
    pass


def _decimal(value, label: str, *, required: bool = True, maximum: Decimal | None = None) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if required:
            raise DebtPayoffValidationError(f"{label} is required.")
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DebtPayoffValidationError(f"{label} must be a number.") from exc
    if not amount.is_finite() or amount < ZERO or (maximum is not None and amount > maximum):
        qualifier = f"between 0 and {maximum}" if maximum is not None else "zero or greater"
        raise DebtPayoffValidationError(f"{label} must be {qualifier}.")
    if amount.adjusted() > 30 or amount.as_tuple().exponent < -18:
        raise DebtPayoffValidationError(f"{label} is outside the supported range.")
    return amount


def _money(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def _amount(value) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
        return result if result.is_finite() else ZERO
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def estimate_minimum_payment(balance, apr_percent) -> Decimal:
    """Estimate max(25 currency units, 1% principal + monthly interest), capped at payoff."""
    principal = abs(_amount(balance)).quantize(CENT, rounding=ROUND_HALF_UP)
    apr = _decimal(apr_percent, "APR", maximum=Decimal("100"))
    if apr is None:
        raise DebtPayoffValidationError("APR is required to estimate a minimum payment.")
    if principal <= ZERO:
        return ZERO
    interest = (principal * apr / Decimal("1200")).quantize(CENT, rounding=ROUND_HALF_UP)
    principal_component = (principal * Decimal("0.01")).quantize(CENT, rounding=ROUND_HALF_UP)
    estimate = max(Decimal("25"), principal_component + interest)
    return min(principal + interest, estimate).quantize(CENT, rounding=ROUND_HALF_UP)


def _parse_apr_response(content: str) -> Decimal:
    text = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DebtPayoffEstimateError("The AI did not return a usable APR estimate. No fields were changed.") from exc
    if not isinstance(payload, dict):
        raise DebtPayoffEstimateError("The AI did not return a usable APR estimate. No fields were changed.")
    try:
        apr = _decimal(payload.get("apr_percent"), "Estimated APR", maximum=Decimal("100"))
    except DebtPayoffValidationError as exc:
        raise DebtPayoffEstimateError("The AI returned an invalid APR estimate. No fields were changed.") from exc
    if apr is None:
        raise DebtPayoffEstimateError("The AI could not estimate an APR for this provider. No fields were changed.")
    return apr


def estimate_debt_terms_with_ai(account: dict) -> dict:
    """Ask the configured LLM for APR using an issuer/display name; compute minimum locally."""
    if not isinstance(account, dict) or account.get("account_type") not in LIABILITY_ACCOUNT_TYPES:
        raise DebtPayoffEstimateError("Choose an account classified as a liability first.")

    account_name = str(account.get("nickname") or account.get("name") or "").strip()
    institution = str(account.get("org") or "").strip()
    if not institution or institution.casefold() in {"unknown", "account"} or institution.casefold() == str(account.get("name") or "").strip().casefold():
        institution = account_name
    if not institution or institution.casefold() in {"unknown", "account"}:
        raise DebtPayoffEstimateError("No institution or account name is available for this estimate.")

    settings = get_ai_settings()
    if not settings["enabled"]:
        raise DebtPayoffEstimateError("Enable AI assistance in Settings before requesting an estimate.")
    provider = settings["provider"]
    model = settings["model"]
    api_key = _provider_key(provider)
    if not api_key:
        raise DebtPayoffEstimateError(f"No API key is saved for the selected AI provider ({provider}).")

    system_message = (
        "Estimate a plausible typical purchase APR for a consumer credit card issued by the named financial institution or account label. "
        "You do not have access to the customer's specific card terms or live web research. Give one cautious, rough APR "
        "estimate between 0 and 100 percent. If the institution is not a recognizable card issuer, use a broad current "
        "consumer-card estimate rather than claiming product-specific knowledge. Return only JSON: "
        '{"apr_percent": number, "note": "short uncertainty caveat"}. Do not request or infer any customer balance.'
    )
    messages = [
        {"role": "system", "content": system_message},
        {"role": "user", "content": json.dumps({"institution": institution}, separators=(",", ":"))},
    ]
    try:
        from ai_chat import _completion, _unsupported_params

        try:
            response = _completion(model, api_key, messages)
        except Exception as exc:
            unsupported = _unsupported_params(exc) if type(exc).__name__ == "UnsupportedParamsError" else []
            if type(exc).__name__ != "UnsupportedParamsError" or (unsupported and "temperature" not in unsupported):
                raise
            response = _completion(model, api_key, messages, omit_temperature=True)
        content = response.choices[0].message.content or ""
        apr = _parse_apr_response(content)
    except DebtPayoffEstimateError:
        raise
    except Exception as exc:
        try:
            from ai_chat import _provider_status_code, _safe_provider_error
            message, _diagnostic = _safe_provider_error(provider, model, _provider_status_code(exc), exc)
        except Exception:
            message = "The configured AI provider could not return an estimate. No fields were changed."
        logger.warning("Debt payoff APR estimate failed (provider=%s, error_type=%s).", provider, type(exc).__name__)
        raise DebtPayoffEstimateError(message) from None

    balance = abs(_amount(account.get("balance")))
    minimum = estimate_minimum_payment(balance, apr)
    return {
        "apr_percent": _money(apr),
        "minimum_payment": _money(minimum),
        "ai_provider": provider,
        "formula": "max(25 currency units, 1% of balance + one month of interest), capped at balance plus interest",
        "note": "Rough, unverified estimate. Check your card statement; issuer formulas, fees, and product terms vary.",
    }


def _load_settings() -> dict:
    raw = get_setting(SETTINGS_KEY)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _display_name(account: dict) -> str:
    return str(account.get("nickname") or account.get("name") or "Account")


def _normalized_accounts(accounts: list[dict]) -> tuple[list[dict], list[dict]]:
    funding = []
    liabilities = []
    for account in accounts:
        kind = account.get("account_type")
        item = {
            "id": str(account.get("id") or ""),
            "name": _display_name(account),
            "currency": str(account.get("currency") or "USD").upper(),
            "balance": _money(abs(_amount(account.get("balance")))) or "0.00",
            "account_type": kind,
        }
        if kind == "bank_account":
            funding.append(item)
        elif kind in LIABILITY_ACCOUNT_TYPES:
            liabilities.append(item)
    return funding, liabilities


def _deposit_average(account_id: str, *, today: date, history: dict | None) -> dict:
    start_date = _subtract_months(today, 6)
    start_ts = int(datetime.combine(start_date, time.min, timezone.utc).timestamp())
    end_ts = int(datetime.combine(today + timedelta(days=1), time.min, timezone.utc).timestamp())
    db = SessionLocal()
    deposits = ZERO
    deposit_count = 0
    try:
        rows = db.query(Transaction).filter(
            Transaction.account_id == str(account_id),
            Transaction.date >= start_ts,
            Transaction.date < end_ts,
            Transaction.pending.is_(False),
        ).all()
        for row in rows:
            amount = _amount(row.amount)
            if amount > ZERO:
                deposits += amount
                deposit_count += 1
    finally:
        db.close()

    history_row = next(
        (item for item in (history or {}).get("accounts", []) if str(item.get("account_id")) == str(account_id)),
        None,
    )
    gaps = (history_row or {}).get("gaps", [])
    has_gap = any(
        int(gap.get("start_ts") or 0) < end_ts and int(gap.get("end_ts") or 0) > start_ts
        for gap in gaps
    )
    history_complete = bool(history_row is not None and not has_gap)
    average = (deposits / Decimal("6")).quantize(CENT, rounding=ROUND_HALF_UP)
    return {
        "monthly_average": _money(average),
        "total_deposits": _money(deposits),
        "deposit_count": deposit_count,
        "window_start": start_date.isoformat(),
        "window_end": today.isoformat(),
        "window_months": 6,
        "history_complete": history_complete,
        "history_stale": bool(history_row.get("stale")) if history_row else True,
    }


def _strategy_order(debts: list[dict], strategy: str, manual_order: list[str]) -> list[dict]:
    if strategy == "lowest_balance":
        return sorted(debts, key=lambda debt: (Decimal(debt["balance"]), debt["name"].casefold(), debt["id"]))
    if strategy == "highest_apr":
        return sorted(debts, key=lambda debt: (-Decimal(debt["apr_percent"] or "0"), debt["name"].casefold(), debt["id"]))
    if strategy == "manual":
        rank = {str(account_id): index for index, account_id in enumerate(manual_order)}
        return sorted(debts, key=lambda debt: (rank.get(debt["id"], len(rank)), debt["name"].casefold(), debt["id"]))
    return sorted(debts, key=lambda debt: (-Decimal(debt["balance"]), debt["name"].casefold(), debt["id"]))


def _month_date(start: date, month_offset: int) -> date:
    month_index = start.year * 12 + start.month - 1 + month_offset
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def _simulate(debts: list[dict], budget: Decimal, ordered_ids: list[str], today: date) -> dict:
    balances = {debt["id"]: Decimal(debt["balance"]).quantize(CENT) for debt in debts}
    terms = {debt["id"]: debt for debt in debts}
    initial_total = sum(balances.values(), ZERO)
    monthly_minimum = sum(
        min(balances[debt["id"]], Decimal(debt["minimum_payment"]))
        for debt in debts
    )
    if budget < monthly_minimum:
        return {
            "status": "below_minimums",
            "minimum_total": _money(monthly_minimum),
            "projected_months": None,
            "projected_payoff_date": None,
            "total_interest": None,
            "debt_results": [],
        }

    paid_off_month: dict[str, int] = {}
    total_interest = ZERO
    for month in range(1, MAX_PROJECTION_MONTHS + 1):
        due_by_id = {}
        for account_id, balance in balances.items():
            if balance <= ZERO:
                continue
            interest = (balance * Decimal(terms[account_id]["apr_percent"]) / Decimal("1200")).quantize(CENT, rounding=ROUND_HALF_UP)
            total_interest += interest
            due_by_id[account_id] = balance + interest

        actual_minimums = ZERO
        for account_id, due in due_by_id.items():
            payment = min(due, Decimal(terms[account_id]["minimum_payment"]))
            balances[account_id] = due - payment
            actual_minimums += payment

        extra = max(ZERO, budget - actual_minimums)
        for account_id in ordered_ids:
            if extra <= ZERO:
                break
            if balances.get(account_id, ZERO) <= ZERO:
                continue
            payment = min(balances[account_id], extra)
            balances[account_id] -= payment
            extra -= payment

        for account_id, balance in balances.items():
            if balance <= ZERO:
                balances[account_id] = ZERO
                paid_off_month.setdefault(account_id, month)
        if sum(balances.values(), ZERO) <= ZERO:
            break

    projected_months = max(paid_off_month.values(), default=None)
    complete = projected_months is not None
    debt_results = []
    for debt in debts:
        payoff_month = paid_off_month.get(debt["id"])
        debt_results.append({
            "account_id": debt["id"],
            "name": debt["name"],
            "starting_balance": debt["balance"],
            "payoff_month": payoff_month,
            "payoff_date": _month_date(today, payoff_month).isoformat() if payoff_month else None,
            "paid_off": payoff_month is not None,
        })
    debt_results.sort(key=lambda item: item["payoff_month"] or MAX_PROJECTION_MONTHS + 1)
    return {
        "status": "projected" if complete else "beyond_horizon",
        "minimum_total": _money(monthly_minimum),
        "projected_months": projected_months,
        "projected_payoff_date": _month_date(today, projected_months).isoformat() if complete else None,
        "total_interest": _money(total_interest) if complete else None,
        "debt_results": debt_results,
    }


def get_debt_payoff_plan(accounts: list[dict], history: dict | None = None, *, today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    settings = _load_settings()
    funding_accounts, liabilities = _normalized_accounts(accounts)
    funding_id = str(settings.get("funding_account_id") or "")
    funding = next((account for account in funding_accounts if account["id"] == funding_id), None)
    selected_terms = settings.get("debt_terms") if isinstance(settings.get("debt_terms"), dict) else {}
    all_liabilities = []
    for item in liabilities:
        terms = selected_terms.get(item["id"], {})
        all_liabilities.append({
            **item,
            "apr_percent": str(terms.get("apr_percent") or "").strip(),
            "minimum_payment": str(terms.get("minimum_payment") or "").strip(),
        })
    active_liabilities = [item for item in all_liabilities if Decimal(item["balance"]) > ZERO]
    all_liability_ids = {item["id"] for item in all_liabilities}
    stored_included = settings.get("included_account_ids")
    if isinstance(stored_included, list):
        included_ids = list(dict.fromkeys(str(item) for item in stored_included if str(item) in all_liability_ids))
    else:
        # Existing planner setups default to active credit cards; other loans are opt-in.
        included_ids = [item["id"] for item in active_liabilities if item["account_type"] == "credit_card"]
    included_id_set = set(included_ids)
    available_same_currency = [
        debt for debt in active_liabilities
        if funding and debt["currency"] == funding["currency"]
    ]
    same_currency_debts = [
        debt for debt in available_same_currency
        if debt["id"] in included_id_set
    ]
    debts = []
    missing_terms = []
    for item in same_currency_debts:
        apr = item["apr_percent"]
        minimum = item["minimum_payment"]
        if Decimal(item["balance"]) > ZERO and (not apr or not minimum):
            missing_terms.append(item["id"])
        debts.append({**item, "apr_percent": apr, "minimum_payment": minimum})

    strategy = settings.get("strategy") if settings.get("strategy") in STRATEGIES else "highest_balance"
    manual_order = settings.get("manual_order") if isinstance(settings.get("manual_order"), list) else []
    strategy_order = _strategy_order(debts, strategy, manual_order)
    saved_manual_order = [str(item) for item in manual_order if str(item) in included_id_set]
    saved_manual_order += [item for item in included_ids if item not in saved_manual_order]
    configured_target_id = str(settings.get("current_target_account_id") or "")
    active_ids = {debt["id"] for debt in debts if Decimal(debt["balance"]) > ZERO}
    target_id = configured_target_id if configured_target_id in active_ids else ""
    effective_target_id = target_id or next((debt["id"] for debt in strategy_order if debt["id"] in active_ids), "")
    payoff_order = ([effective_target_id] if effective_target_id else []) + [
        debt["id"] for debt in strategy_order if debt["id"] != effective_target_id
    ]
    order_rank = {account_id: index for index, account_id in enumerate(payoff_order)}
    debts.sort(key=lambda debt: order_rank.get(debt["id"], len(order_rank)))

    deposit_data = _deposit_average(funding_id, today=today, history=history) if funding else None
    override = _decimal(settings.get("monthly_budget_override"), "Monthly payment override", required=False)
    monthly_budget = override if override is not None else _amount(deposit_data.get("monthly_average")) if deposit_data else None
    total_balance = sum((Decimal(debt["balance"]) for debt in debts), ZERO)
    minimum_total = sum((
        min(Decimal(debt["balance"]), _amount(debt["minimum_payment"]))
        for debt in debts if debt["minimum_payment"] and Decimal(debt["balance"]) > ZERO
    ), ZERO)
    baseline_map = settings.get("baseline_balances") if isinstance(settings.get("baseline_balances"), dict) else {}
    progress_debts = [
        debt for debt in all_liabilities
        if funding and debt["currency"] == funding["currency"] and debt["id"] in included_id_set
    ]
    starting_total = sum((_amount(baseline_map.get(debt["id"], debt["balance"])) for debt in progress_debts), ZERO)
    debt_paid = max(ZERO, starting_total - total_balance)
    progress = Decimal("100") if starting_total == ZERO and total_balance == ZERO else (
        max(ZERO, min(Decimal("100"), debt_paid * 100 / starting_total)) if starting_total > ZERO else ZERO
    )
    milestones = [
        {"percent": percent, "achieved": progress >= Decimal(percent)}
        for percent in (25, 50, 75, 100)
    ]

    if not funding:
        simulation = {"status": "setup_required", "minimum_total": None, "projected_months": None, "projected_payoff_date": None, "total_interest": None, "debt_results": []}
    elif not available_same_currency:
        simulation = {"status": "no_debts", "minimum_total": "0.00", "projected_months": 0, "projected_payoff_date": today.isoformat(), "total_interest": "0.00", "debt_results": []}
    elif not debts:
        simulation = {"status": "no_included_debts", "minimum_total": "0.00", "projected_months": None, "projected_payoff_date": None, "total_interest": None, "debt_results": []}
    elif not target_id:
        simulation = {"status": "setup_required", "minimum_total": _money(minimum_total), "projected_months": None, "projected_payoff_date": None, "total_interest": None, "debt_results": []}
    elif missing_terms:
        simulation = {"status": "needs_terms", "minimum_total": _money(minimum_total), "projected_months": None, "projected_payoff_date": None, "total_interest": None, "debt_results": []}
    elif monthly_budget is None or monthly_budget <= ZERO:
        simulation = {"status": "no_budget", "minimum_total": _money(minimum_total), "projected_months": None, "projected_payoff_date": None, "total_interest": None, "debt_results": []}
    else:
        simulation = _simulate(debts, monthly_budget, payoff_order, today)

    available_target = next((item for item in same_currency_debts if item["id"] == effective_target_id), None)
    return {
        "settings": {
            "funding_account_id": funding_id if funding else "",
            "current_target_account_id": target_id if target_id in {debt["id"] for debt in same_currency_debts} else "",
            "strategy": strategy,
            "monthly_budget_override": _money(override),
            "manual_order": [str(item) for item in saved_manual_order],
            "included_account_ids": included_ids,
        },
        "funding_accounts": funding_accounts,
        "liabilities": active_liabilities,
        "currency": funding["currency"] if funding else None,
        "funding_account_name": funding["name"] if funding else None,
        "current_target_name": available_target["name"] if available_target else None,
        "effective_target_account_id": effective_target_id,
        "payoff_order": payoff_order,
        "deposit_average": deposit_data,
        "monthly_budget": _money(monthly_budget),
        "total_balance": _money(total_balance),
        "total_minimum_payment": _money(minimum_total),
        "starting_total_balance": _money(starting_total),
        "debt_paid": _money(debt_paid),
        "progress_percent": round(float(progress), 1),
        "milestones": milestones,
        "missing_term_account_ids": missing_terms,
        "history_warning": bool(deposit_data and (not deposit_data["history_complete"] or deposit_data["history_stale"])),
        **simulation,
    }


def save_debt_payoff_settings(payload: dict, accounts: list[dict]) -> dict:
    if not isinstance(payload, dict):
        raise DebtPayoffValidationError("Planner settings must be an object.")
    funding_accounts, liabilities = _normalized_accounts(accounts)
    funding_id = str(payload.get("funding_account_id") or "").strip()
    funding = next((item for item in funding_accounts if item["id"] == funding_id), None)
    if not funding:
        raise DebtPayoffValidationError("Choose an existing bank / cash account for payment deposits.")
    active_eligible = [
        item for item in liabilities
        if item["currency"] == funding["currency"] and Decimal(item["balance"]) > ZERO
    ]
    active_eligible_ids = {item["id"] for item in active_eligible}
    all_liability_ids = {item["id"] for item in liabilities}
    raw_included = payload.get("included_account_ids")
    if not isinstance(raw_included, list) or len(raw_included) > len(all_liability_ids):
        raise DebtPayoffValidationError("Choose valid debts to include in the payoff plan.")
    included_ids = [str(item) for item in raw_included]
    if len(included_ids) != len(set(included_ids)) or any(item not in all_liability_ids for item in included_ids):
        raise DebtPayoffValidationError("Choose valid, unique liability accounts for the payoff plan.")
    included_id_set = set(included_ids)
    included_active_ids = active_eligible_ids & included_id_set
    if not included_active_ids:
        raise DebtPayoffValidationError("Include at least one outstanding debt in the payoff plan.")
    target_id = str(payload.get("current_target_account_id") or "").strip()
    if target_id not in included_active_ids:
        raise DebtPayoffValidationError("Choose an included outstanding debt in the same currency as the funding account.")
    strategy = str(payload.get("strategy") or "highest_balance")
    if strategy not in STRATEGIES:
        raise DebtPayoffValidationError("Choose a supported payoff order.")
    override = _decimal(payload.get("monthly_budget_override"), "Monthly payment override", required=False)
    if override is not None and override <= ZERO:
        raise DebtPayoffValidationError("Monthly payment override must be greater than zero, or left blank.")

    terms_payload = payload.get("debt_terms") or []
    if not isinstance(terms_payload, list) or len(terms_payload) > 100:
        raise DebtPayoffValidationError("Debt APR and minimum payment details are invalid.")
    settings = _load_settings()
    existing_terms = settings.get("debt_terms") if isinstance(settings.get("debt_terms"), dict) else {}
    terms = {
        str(account_id): value
        for account_id, value in existing_terms.items()
        if isinstance(value, dict)
    }
    submitted_term_ids = set()
    for row in terms_payload:
        if not isinstance(row, dict):
            raise DebtPayoffValidationError("Debt APR and minimum payment details are invalid.")
        account_id = str(row.get("account_id") or "").strip()
        if account_id not in included_active_ids or account_id in submitted_term_ids:
            raise DebtPayoffValidationError("APR and minimum details must match included outstanding debts.")
        submitted_term_ids.add(account_id)
        apr = _decimal(row.get("apr_percent"), "APR", required=False, maximum=Decimal("100"))
        minimum = _decimal(row.get("minimum_payment"), "Minimum payment", required=False)
        account = next(item for item in active_eligible if item["id"] == account_id)
        if Decimal(account["balance"]) > ZERO and minimum == ZERO:
            raise DebtPayoffValidationError("Minimum payment must be greater than zero for an outstanding debt.")
        terms[account_id] = {
            "apr_percent": _money(apr),
            "minimum_payment": _money(minimum),
        }

    manual_order = payload.get("manual_order") or []
    if not isinstance(manual_order, list) or len(manual_order) > len(included_ids):
        raise DebtPayoffValidationError("Manual payoff order is invalid.")
    manual_order = [str(item) for item in manual_order]
    if len(manual_order) != len(set(manual_order)) or any(item not in included_id_set for item in manual_order):
        raise DebtPayoffValidationError("Manual payoff order must contain unique included liability accounts.")
    previous_order = settings.get("manual_order") if isinstance(settings.get("manual_order"), list) else []
    manual_order += [
        str(item) for item in previous_order
        if str(item) in included_id_set and str(item) not in manual_order
    ]
    manual_order += [account_id for account_id in included_ids if account_id not in manual_order]

    baseline = settings.get("baseline_balances") if isinstance(settings.get("baseline_balances"), dict) else {}
    for debt in liabilities:
        if debt["id"] not in included_id_set:
            continue
        if debt["id"] not in baseline:
            baseline[debt["id"]] = debt["balance"]
    normalized = {
        "funding_account_id": funding_id,
        "current_target_account_id": target_id,
        "strategy": strategy,
        "monthly_budget_override": _money(override),
        "debt_terms": terms,
        "manual_order": manual_order,
        "included_account_ids": included_ids,
        "baseline_balances": baseline,
    }
    set_setting(SETTINGS_KEY, json.dumps(normalized, separators=(",", ":"), sort_keys=True))
    return normalized
