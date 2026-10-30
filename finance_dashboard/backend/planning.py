"""User-editable planning assumptions and BLS CPI-based inflation estimates."""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen

from database import get_setting, set_setting

PLANNING_SETTINGS_KEY = "retirement_planning_settings"
BLS_CPI_SERIES = "CUUR0000SA0"  # CPI-U, U.S. city average, all items, not seasonally adjusted.
BLS_API_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
ALLOWED_INFLATION_WINDOWS = {5, 10, 20}

DEFAULT_PLANNING_SETTINGS = {
    "birth_year": None,
    "expected_return_percent": "6",
    "withdrawal_rate_percent": "4",
    "inflation_percent": "2.5",
    "inflation_source": "default",
    "inflation_window_years": 10,
    "inflation_as_of": None,
    "inflation_synced_at": None,
    "inflation_latest_yoy_percent": None,
}


class PlanningSettingsError(ValueError):
    pass


def _rate(value, label: str, minimum: str, maximum: str) -> str:
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PlanningSettingsError(f"{label} must be a number.") from exc
    if not number.is_finite() or number < Decimal(minimum) or number > Decimal(maximum):
        raise PlanningSettingsError(f"{label} must be between {minimum}% and {maximum}%.")
    return format(number.normalize(), "f")


def get_planning_settings() -> dict:
    raw = get_setting(PLANNING_SETTINGS_KEY)
    if not raw:
        return dict(DEFAULT_PLANNING_SETTINGS)
    try:
        saved = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        saved = {}
    settings = dict(DEFAULT_PLANNING_SETTINGS)
    if isinstance(saved, dict):
        settings.update({key: value for key, value in saved.items() if key in settings})
    return settings


def save_planning_settings(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise PlanningSettingsError("Planning settings must be an object.")
    current = get_planning_settings()
    birth_year = payload.get("birth_year", current["birth_year"])
    if birth_year in (None, ""):
        birth_year = None
    else:
        try:
            birth_year = int(birth_year)
        except (TypeError, ValueError) as exc:
            raise PlanningSettingsError("Birth year must be a whole year.") from exc
        if birth_year < 1900 or birth_year > date.today().year:
            raise PlanningSettingsError("Enter a valid birth year.")

    try:
        window = int(payload.get("inflation_window_years", current["inflation_window_years"]))
    except (TypeError, ValueError) as exc:
        raise PlanningSettingsError("Choose a supported inflation averaging window.") from exc
    if window not in ALLOWED_INFLATION_WINDOWS:
        raise PlanningSettingsError("Choose a 5, 10, or 20 year inflation window.")

    source = str(payload.get("inflation_source", current["inflation_source"]) or "manual")
    if source not in {"manual", "bls", "default"}:
        raise PlanningSettingsError("Choose BLS data or a manual inflation assumption.")

    settings = {
        **current,
        "birth_year": birth_year,
        "expected_return_percent": _rate(payload.get("expected_return_percent", current["expected_return_percent"]), "Expected annual return", "0", "50"),
        "withdrawal_rate_percent": _rate(payload.get("withdrawal_rate_percent", current["withdrawal_rate_percent"]), "Withdrawal rate", "0.1", "20"),
        "inflation_percent": _rate(payload.get("inflation_percent", current["inflation_percent"]), "Inflation assumption", "0", "30"),
        "inflation_window_years": window,
        "inflation_source": source,
    }
    if source == "manual":
        settings["inflation_as_of"] = None
        settings["inflation_synced_at"] = None
        settings["inflation_latest_yoy_percent"] = None
    set_setting(PLANNING_SETTINGS_KEY, json.dumps(settings, separators=(",", ":")))
    return settings


def _fetch_bls_series(start_year: int, end_year: int) -> list[dict]:
    body = json.dumps({
        "seriesid": [BLS_CPI_SERIES],
        "startyear": str(start_year),
        "endyear": str(end_year),
    }).encode("utf-8")
    request = Request(BLS_API_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise PlanningSettingsError("BLS did not return CPI data. Try syncing again later.")
    series = ((payload.get("Results") or {}).get("series") or [])
    if not series or not isinstance(series[0].get("data"), list):
        raise PlanningSettingsError("BLS returned no CPI observations.")
    return series[0]["data"]


def _fetch_window_data(window_years: int, current_year: int) -> dict[tuple[int, int], Decimal]:
    start_year = current_year - window_years
    years = []
    chunk_start = start_year
    while chunk_start <= current_year:
        # The unauthenticated BLS API caps each request at 10 calendar years,
        # so use at most 10 inclusive year labels per request.
        chunk_end = min(current_year, chunk_start + 9)
        years.extend(_fetch_bls_series(chunk_start, chunk_end))
        chunk_start = chunk_end + 1
    observations = {}
    for item in years:
        period = str(item.get("period") or "")
        if not period.startswith("M") or not period[1:].isdigit():
            continue
        month = int(period[1:])
        if not 1 <= month <= 12:
            continue
        try:
            value = Decimal(str(item.get("value")))
            year = int(item.get("year"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if value.is_finite() and value > 0:
            observations[(year, month)] = value
    return observations


def sync_bls_inflation(window_years: int | None = None) -> dict:
    settings = get_planning_settings()
    window = int(window_years if window_years is not None else settings["inflation_window_years"])
    if window not in ALLOWED_INFLATION_WINDOWS:
        raise PlanningSettingsError("Choose a 5, 10, or 20 year inflation window.")
    observations = _fetch_window_data(window, date.today().year)
    if not observations:
        raise PlanningSettingsError("BLS returned no usable monthly CPI data.")

    latest_year, latest_month = max(observations)
    months_old = (date.today().year - latest_year) * 12 + date.today().month - latest_month
    if months_old > 4:
        raise PlanningSettingsError("The latest BLS CPI observation is over four months old. Your saved inflation assumption was not changed.")
    latest = observations[(latest_year, latest_month)]
    base_key = (latest_year - window, latest_month)
    base = observations.get(base_key)
    if base is None:
        raise PlanningSettingsError(
            f"BLS does not have a matching CPI observation {window} years before the latest release."
        )
    annualized = (float(latest / base) ** (1 / window) - 1) * 100
    prior_year = observations.get((latest_year - 1, latest_month))
    latest_yoy = ((float(latest / prior_year) - 1) * 100) if prior_year else None
    if not math.isfinite(annualized) or annualized < -100 or annualized > 100:
        raise PlanningSettingsError("BLS CPI data produced an invalid inflation estimate.")

    settings.update({
        "inflation_window_years": window,
        "inflation_percent": f"{annualized:.2f}",
        "inflation_source": "bls",
        "inflation_as_of": f"{latest_year}-{latest_month:02d}",
        "inflation_synced_at": datetime.now(timezone.utc).isoformat(),
        "inflation_latest_yoy_percent": f"{latest_yoy:.2f}" if latest_yoy is not None else None,
    })
    set_setting(PLANNING_SETTINGS_KEY, json.dumps(settings, separators=(",", ":")))
    return settings
