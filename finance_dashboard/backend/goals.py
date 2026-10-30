"""Encrypted persistence and transparent progress estimates for financial goals."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from uuid import uuid4

from database import get_setting, set_setting
from models import LIABILITY_ACCOUNT_TYPES
from planning import get_planning_settings

GOALS_SETTING_KEY = "financial_goals"
MAX_GOALS = 30
CENT = Decimal("0.01")


class GoalValidationError(ValueError):
    pass


def _decimal(value, label: str, *, required: bool = True) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if required:
            raise GoalValidationError(f"{label} is required.")
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GoalValidationError(f"{label} must be a number.") from exc
    if not result.is_finite():
        raise GoalValidationError(f"{label} must be a finite number.")
    return result


def _money(value, label: str, *, required: bool = True, allow_zero: bool = True) -> str | None:
    result = _decimal(value, label, required=required)
    if result is None:
        return None
    if result < 0 or (not allow_zero and result == 0):
        qualifier = "greater than zero" if not allow_zero else "zero or greater"
        raise GoalValidationError(f"{label} must be {qualifier}.")
    if result.adjusted() > 30 or result.as_tuple().exponent < -18:
        raise GoalValidationError(f"{label} is outside the supported range.")
    return format(result, "f")


def _rate(value, label: str, *, minimum: str = "0", maximum: str = "100") -> str:
    result = _decimal(value, label)
    if result is None or result < Decimal(minimum) or result > Decimal(maximum):
        raise GoalValidationError(f"{label} must be between {minimum}% and {maximum}%.")
    return format(result, "f")


def _source_ids(value, label: str, *, required: bool = True) -> list[str]:
    if not isinstance(value, (list, tuple)):
        if value is None and not required:
            return []
        raise GoalValidationError(f"Choose at least one {label}.")
    ids = [str(item).strip() for item in value if str(item).strip()]
    if (required and not ids) or len(ids) > 30 or len(ids) != len(set(ids)):
        raise GoalValidationError(f"Choose between 1 and 30 unique {label}.")
    return ids


def _resolve_sources(goal_type: str, account_ids: list[str], manual_asset_ids: list[str], accounts, manual_assets):
    account_map = {str(item.get("id")): item for item in accounts}
    manual_map = {str(item.get("id")): item for item in manual_assets}
    selected_accounts = []
    selected_assets = []

    if goal_type == "debt_payoff":
        if len(account_ids) != 1 or manual_asset_ids:
            raise GoalValidationError("A payoff goal must use exactly one liability account.")
        account = account_map.get(account_ids[0])
        if not account or account.get("account_type") not in LIABILITY_ACCOUNT_TYPES:
            raise GoalValidationError("Choose an existing account classified as a liability.")
        selected_accounts.append(account)
    elif goal_type == "savings":
        if manual_asset_ids:
            raise GoalValidationError("Savings goals use linked bank accounts, not manual assets.")
        for account_id in account_ids:
            account = account_map.get(account_id)
            if not account or account.get("account_type") != "bank_account":
                raise GoalValidationError("Choose linked bank accounts for a savings goal.")
            selected_accounts.append(account)
    elif goal_type == "retirement":
        for account_id in account_ids:
            account = account_map.get(account_id)
            if not account or account.get("account_type") != "investment":
                raise GoalValidationError("Choose linked investment accounts for a retirement goal.")
            selected_accounts.append(account)
        for asset_id in manual_asset_ids:
            asset = manual_map.get(asset_id)
            if not asset:
                raise GoalValidationError("A selected manual asset no longer exists.")
            selected_assets.append(asset)
        if not selected_accounts and not selected_assets:
            raise GoalValidationError("Choose at least one investment account or manual asset.")
    else:
        raise GoalValidationError("Choose a supported goal type.")

    currencies = {
        str(item.get("currency") or "USD").upper()
        for item in [*selected_accounts, *selected_assets]
    }
    if len(currencies) != 1:
        raise GoalValidationError("All sources for a goal must use the same currency; currency conversion is not applied.")
    currency = next(iter(currencies))
    if len(currency) != 3 or not currency.isalpha():
        raise GoalValidationError("The selected sources have an unsupported currency code.")
    return selected_accounts, selected_assets, currency


def _source_balance(accounts, assets) -> Decimal:
    total = Decimal("0")
    for item in accounts:
        try:
            amount = Decimal(str(item.get("balance", "0")))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("0")
        if amount.is_finite():
            total += abs(amount) if item.get("account_type") in LIABILITY_ACCOUNT_TYPES else amount
    for item in assets:
        try:
            amount = Decimal(str(item.get("value", "0")))
        except (InvalidOperation, TypeError, ValueError):
            amount = Decimal("0")
        if amount.is_finite():
            total += amount
    return total


def _normalize_payload(payload: dict, accounts, manual_assets) -> dict:
    if not isinstance(payload, dict):
        raise GoalValidationError("Goal data must be an object.")
    goal_type = str(payload.get("goal_type") or "").strip()
    if goal_type not in {"debt_payoff", "savings", "retirement"}:
        raise GoalValidationError("Choose payoff, savings, or retirement as the goal type.")

    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 80:
        raise GoalValidationError("Goal name must contain 1 to 80 characters.")
    manual_value = payload.get("manual_asset_ids", [])
    if manual_value is None:
        manual_value = []
    if not isinstance(manual_value, (list, tuple)):
        raise GoalValidationError("Choose valid manual asset sources.")
    manual_asset_ids = [str(item).strip() for item in manual_value if str(item).strip()]
    if len(manual_asset_ids) > 30 or len(manual_asset_ids) != len(set(manual_asset_ids)):
        raise GoalValidationError("Choose no more than 30 unique manual assets.")
    account_ids = _source_ids(
        payload.get("account_ids"),
        "eligible accounts",
        required=not (goal_type == "retirement" and manual_asset_ids),
    )

    selected_accounts, selected_assets, currency = _resolve_sources(
        goal_type, account_ids, manual_asset_ids, accounts, manual_assets
    )
    normalized = {
        "goal_type": goal_type,
        "name": name,
        "account_ids": account_ids,
        "manual_asset_ids": manual_asset_ids,
        "currency": currency,
    }

    if goal_type == "debt_payoff":
        target_date = str(payload.get("target_date") or "").strip()
        try:
            date.fromisoformat(target_date)
        except ValueError as exc:
            raise GoalValidationError("Choose a valid payoff target date.") from exc
        normalized.update({
            "target_date": target_date,
            "apr_percent": _rate(payload.get("apr_percent"), "Annual APR"),
            "planned_monthly": _money(payload.get("planned_monthly"), "Planned monthly payment", required=False),
            "target_amount": "0",
        })
    elif goal_type == "savings":
        target_date = str(payload.get("target_date") or "").strip() or None
        if target_date:
            try:
                date.fromisoformat(target_date)
            except ValueError as exc:
                raise GoalValidationError("Choose a valid target date.") from exc
        normalized.update({
            "target_date": target_date,
            "target_amount": _money(payload.get("target_amount"), "Target amount", allow_zero=False),
            "planned_monthly": _money(payload.get("planned_monthly"), "Planned monthly contribution", required=False),
        })
    else:
        monthly_income_target = _money(
            payload.get("monthly_income_target"),
            "Monthly retirement income target",
            required=False,
            allow_zero=False,
        )
        withdrawal_rate = _rate(
            payload.get("withdrawal_rate_percent") or get_planning_settings()["withdrawal_rate_percent"],
            "Withdrawal rate",
            minimum="0.1",
            maximum="20",
        )
        if monthly_income_target is not None:
            target_amount = _ceil_money(
                Decimal(monthly_income_target) * Decimal("12") / (Decimal(withdrawal_rate) / Decimal("100"))
            )
            normalized["monthly_income_target"] = monthly_income_target
        else:
            target_amount = _money(payload.get("target_amount"), "Retirement target amount", allow_zero=False)
        normalized["target_amount"] = format(Decimal(str(target_amount)), "f")
        normalized["withdrawal_rate_percent"] = withdrawal_rate
        timing_mode = str(payload.get("retirement_timing_mode") or "").strip()
        if timing_mode == "age":
            try:
                birth_year = int(payload.get("birth_year"))
                target_age = int(payload.get("target_retirement_age"))
            except (TypeError, ValueError) as exc:
                raise GoalValidationError("Enter a birth year and target retirement age.") from exc
            current_year = date.today().year
            if birth_year < 1900 or birth_year > current_year or target_age < 18 or target_age > 100:
                raise GoalValidationError("Enter a valid birth year and target age between 18 and 100.")
            retirement_year = birth_year + target_age
            normalized.update({
                "retirement_timing_mode": "age",
                "birth_year": birth_year,
                "target_retirement_age": target_age,
                "retirement_year": retirement_year,
            })
        elif timing_mode == "year":
            try:
                retirement_year = int(payload.get("retirement_year"))
            except (TypeError, ValueError) as exc:
                raise GoalValidationError("Enter a retirement year.") from exc
            if retirement_year < 1900 or retirement_year > 2200:
                raise GoalValidationError("Retirement year must be between 1900 and 2200.")
            normalized.update({
                "retirement_timing_mode": "year",
                "birth_year": None,
                "target_retirement_age": None,
                "retirement_year": retirement_year,
            })
        else:
            raise GoalValidationError("Choose birth year and target age, or a retirement year.")
        normalized.update({
            "target_date": f"{retirement_year:04d}-12-31",
            "expected_return_percent": _rate(payload.get("expected_return_percent"), "Expected annual return", maximum="50"),
            "inflation_percent": _rate(payload.get("inflation_percent"), "Annual inflation", maximum="30"),
            "planned_monthly": _money(payload.get("planned_monthly"), "Planned monthly contribution", required=False),
        })

    normalized["starting_balance"] = format(_source_balance(selected_accounts, selected_assets), "f")
    return normalized


def _load_records() -> list[dict]:
    raw = get_setting(GOALS_SETTING_KEY)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [record for record in data if isinstance(record, dict)] if isinstance(data, list) else []


def _save_records(records: list[dict]):
    set_setting(GOALS_SETTING_KEY, json.dumps(records, separators=(",", ":")))


def create_goal(payload: dict, accounts, manual_assets) -> dict:
    records = _load_records()
    if len(records) >= MAX_GOALS:
        raise GoalValidationError(f"You can have up to {MAX_GOALS} goals.")
    goal = _normalize_payload(payload, accounts, manual_assets)
    goal.update({"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat()})
    records.append(goal)
    _save_records(records)
    return _calculate_goal(goal, accounts, manual_assets)


def update_goal(goal_id: str, payload: dict, accounts, manual_assets) -> dict | None:
    records = _load_records()
    index = next((i for i, record in enumerate(records) if record.get("id") == str(goal_id)), None)
    if index is None:
        return None
    existing = records[index]
    goal = _normalize_payload(payload, accounts, manual_assets)
    if (
        goal["goal_type"] == existing.get("goal_type")
        and goal["account_ids"] == existing.get("account_ids")
        and goal["manual_asset_ids"] == existing.get("manual_asset_ids")
    ):
        goal["starting_balance"] = existing.get("starting_balance", goal["starting_balance"])
    goal.update({"id": str(goal_id), "created_at": existing.get("created_at")})
    records[index] = goal
    _save_records(records)
    return _calculate_goal(goal, accounts, manual_assets)


def delete_goal(goal_id: str) -> bool:
    records = _load_records()
    remaining = [record for record in records if record.get("id") != str(goal_id)]
    if len(remaining) == len(records):
        return False
    _save_records(remaining)
    return True


def get_goals(accounts, manual_assets, *, today: date | None = None) -> list[dict]:
    today = today or date.today()
    return [_calculate_goal(record, accounts, manual_assets, today=today) for record in _load_records()]


def _decimal_or_zero(value) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
        return result if result.is_finite() else Decimal("0")
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _ceil_money(value: float | Decimal) -> Decimal:
    try:
        amount = Decimal(str(value))
        with localcontext() as context:
            context.prec = max(28, amount.adjusted() + 3, len(amount.as_tuple().digits) + 2)
            return amount.quantize(CENT, rounding=ROUND_CEILING)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _months_until(target: date, today: date) -> int:
    months = (target.year - today.year) * 12 + target.month - today.month
    if target.day > today.day:
        months += 1
    return max(0, months)


def _format_amount(value: Decimal | None) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = max(28, value.adjusted() + 3, len(value.as_tuple().digits) + 2)
        return format(value.quantize(CENT), "f")


def _source_names(goal: dict, accounts, manual_assets) -> list[str]:
    account_map = {str(item.get("id")): item for item in accounts}
    asset_map = {str(item.get("id")): item for item in manual_assets}
    names = []
    for account_id in goal.get("account_ids", []):
        account = account_map.get(str(account_id))
        if account:
            names.append(str(account.get("nickname") or account.get("name") or "Account"))
    for asset_id in goal.get("manual_asset_ids", []):
        asset = asset_map.get(str(asset_id))
        if asset:
            names.append(str(asset.get("name") or "Manual asset"))
    return names


def _calculate_goal(goal: dict, accounts, manual_assets, *, today: date | None = None) -> dict:
    today = today or date.today()
    account_map = {str(item.get("id")): item for item in accounts}
    asset_map = {str(item.get("id")): item for item in manual_assets}
    source_accounts = [account_map[item] for item in goal.get("account_ids", []) if item in account_map]
    source_assets = [asset_map[item] for item in goal.get("manual_asset_ids", []) if item in asset_map]
    current = _source_balance(source_accounts, source_assets)
    kind = goal.get("goal_type")
    target = _decimal_or_zero(goal.get("target_amount"))
    planned = _decimal_or_zero(goal.get("planned_monthly")) if goal.get("planned_monthly") is not None else None
    start = _decimal_or_zero(goal.get("starting_balance"))
    target_date = date.fromisoformat(goal["target_date"]) if goal.get("target_date") else None
    months = _months_until(target_date, today) if target_date else None
    required = None
    projected = None
    target_nominal = target
    progress_amount = current
    remaining = max(Decimal("0"), target - current)
    status = "progressing"

    if kind == "debt_payoff":
        remaining = current
        progress_amount = max(Decimal("0"), start - current)
        progress_pct = Decimal("100") if start == 0 or current <= 0 else max(
            Decimal("0"), min(Decimal("100"), progress_amount * 100 / start)
        )
        if current <= 0:
            status = "completed"
        elif months == 0:
            status = "overdue"
        else:
            apr = float(_decimal_or_zero(goal.get("apr_percent"))) / 100 / 12
            principal = float(current)
            if apr == 0:
                required = _ceil_money(current / months)
            else:
                factor = math.pow(1 + apr, months)
                required = _ceil_money(principal * apr * factor / (factor - 1))
            if planned is None:
                status = "needs_plan"
            elif planned >= required:
                status = "on_track"
            else:
                status = "needs_attention"
    elif kind == "savings":
        progress_pct = Decimal("100") if target == 0 else max(
            Decimal("0"), min(Decimal("100"), current * 100 / target)
        )
        if current >= target:
            status = "completed"
        elif months == 0:
            status = "overdue"
        elif months:
            required = _ceil_money(remaining / months)
            if planned is None:
                status = "needs_plan"
            else:
                projected = current + planned * months
                status = "on_track" if projected >= target else "needs_attention"
    else:
        progress_pct = Decimal("100") if target == 0 else max(
            Decimal("0"), min(Decimal("100"), current * 100 / target)
        )
        retirement_year = int(goal.get("retirement_year") or today.year)
        months = _months_until(date(retirement_year, 12, 31), today)
        return_rate = float(_decimal_or_zero(goal.get("expected_return_percent"))) / 100 / 12
        inflation_rate = float(_decimal_or_zero(goal.get("inflation_percent"))) / 100
        years = months / 12
        target_nominal = _ceil_money(float(target) * math.pow(1 + inflation_rate, years))
        if months > 0:
            growth_factor = math.pow(1 + return_rate, months)
            annuity_factor = (growth_factor - 1) / return_rate if return_rate else float(months)
            future_current = float(current) * growth_factor
            gap = max(0.0, float(target_nominal) - future_current)
            required = _ceil_money(gap / annuity_factor) if annuity_factor > 0 else Decimal("0")
            if planned is not None:
                projected = _ceil_money(future_current + float(planned) * annuity_factor)
        if current >= target_nominal or (months > 0 and projected is not None and projected >= target_nominal):
            status = "completed" if current >= target_nominal else "on_track"
        elif months == 0:
            status = "overdue"
        elif planned is None:
            status = "needs_plan"
        else:
            status = "needs_attention"
        remaining = max(Decimal("0"), target - current)

    encouragement = _encouragement(kind, status, current, remaining, required, planned, goal, target_date, today)
    return {
        **goal,
        "source_names": _source_names(goal, accounts, manual_assets),
        "current_amount": _format_amount(current),
        "progress_amount": _format_amount(progress_amount),
        "remaining_amount": _format_amount(remaining),
        "progress_percent": round(float(progress_pct), 1),
        "months_remaining": months,
        "required_monthly": _format_amount(required),
        "projected_amount": _format_amount(projected),
        "target_nominal_amount": _format_amount(target_nominal),
        "status": status,
        "encouragement": encouragement,
    }


def _encouragement(kind, status, current, remaining, required, planned, goal, target_date, today):
    currency = goal.get("currency", "USD")
    if status == "completed":
        return "You reached this goal. Take a moment to celebrate that progress."
    if status == "overdue":
        return "Your target date has passed. Update the date to make a fresh plan; your progress is still here."
    if status == "needs_plan":
        if required is not None:
            return f"You’re making progress. An estimated {currency} {required:,.2f} per month reaches your target on time, using the assumptions shown."
        return f"You’re {currency} {remaining:,.2f} from this goal. Add a target date or monthly plan to see a pace estimate."
    if status == "on_track":
        return "Your planned pace is on track for this goal. Keep going—you’re building toward it."
    if status == "needs_attention" and required is not None:
        return f"You’re making progress. An estimated {currency} {required:,.2f} per month is the pace for your target date."
    if kind == "savings":
        return f"You’re {currency} {remaining:,.2f} from your savings target. Every step adds up."
    if kind == "retirement":
        return "Your current balance is recorded. Add a planned contribution to compare your projection with the target."
    return "Your progress is saved."
