import logging
import json
import os
import re
import select
import sys
import threading
from collections import Counter, defaultdict
from typing import Literal
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import simplefin
from app_logging import apply_app_log_level, get_app_log_settings
from ai_assistant import (
    ProviderModelDiscoveryError,
    _provider_key,
    build_ai_category_defaults,
    get_ai_settings,
    list_provider_models,
    update_ai_settings,
)
from ai_chat import (
    AIChatError,
    AIProviderRequestError,
    create_goal_chat_response,
    prepare_goal_chat_preview,
)
from ai_logging import redact_api_keys
from ai_prompts import AIPromptValidationError, get_ai_prompt_settings, save_ai_system_prompt
from category_analysis import generate_category_analysis
from recurring_analysis import generate_recurring_analysis
from goals import GoalValidationError, create_goal, delete_goal, get_goals, update_goal
from debt_payoff import (
    DebtPayoffEstimateError,
    DebtPayoffValidationError,
    estimate_debt_terms_with_ai,
    get_debt_payoff_plan,
    save_debt_payoff_settings,
)
from planning import PlanningSettingsError, get_planning_settings, save_planning_settings, sync_bls_inflation
from database import (
    init_db,
    get_stored_accounts,
    get_stored_transactions,
    get_manual_assets,
    create_manual_asset,
    update_manual_asset,
    delete_manual_asset,
    get_category_rules_value,
    get_recurring_rules_value,
    set_category_rules_value,
    set_recurring_rules_value,
    set_account_type,
    set_account_nickname,
    get_transaction_count,
    set_transaction_category,
)
import pin_auth
from models import SessionLocal, Transaction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finance_dashboard")

app = FastAPI(title="Finance Dashboard API")


@app.middleware("http")
async def prevent_stale_dashboard_html(request, call_next):
    response = await call_next(request)
    if request.method == "GET" and request.url.path in {"/", "/index.html"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

FINANCE_STDIN_MAX_BYTES = 65536
FINANCE_AUDIT_PAGE_SIZE = 100
_periodic_sync_stop = threading.Event()
_periodic_sync_thread = None
_periodic_sync_lock = threading.Lock()
_simplefin_sync_lock = threading.Lock()
_startup_sync_thread = None
_startup_sync_lock = threading.Lock()
_history_retry_thread = None
_history_retry_lock = threading.Lock()


def _serialized_simplefin_sync(source: str, *, wait: bool = False):
    if not _simplefin_sync_lock.acquire(blocking=wait):
        logger.info("Skipping %s SimpleFIN sync; another sync is still running.", source)
        return {
            "status": "in_progress",
            "message": "A SimpleFIN sync is already running. Its results will appear when it finishes.",
        }
    try:
        logger.info("Starting %s SimpleFIN sync.", source)
        return simplefin.fetch_and_sync_simplefin()
    finally:
        _simplefin_sync_lock.release()


def _run_startup_sync():
    try:
        result = _serialized_simplefin_sync("startup", wait=True)
        if result.get("status") == "in_progress":
            logger.info("Startup SimpleFIN sync paused: %s", result.get("message", "request budget reached"))
        elif result.get("status") != "success":
            logger.error("Startup SimpleFIN sync did not complete: %s", result.get("message", "unknown error"))
    except Exception as exc:
        logger.exception("Startup SimpleFIN sync failed: %s", exc)


def _start_startup_sync():
    global _startup_sync_thread
    with _startup_sync_lock:
        if _startup_sync_thread and _startup_sync_thread.is_alive():
            return
        _startup_sync_thread = threading.Thread(
            target=_run_startup_sync,
            name="finance-simplefin-startup-sync",
            daemon=True,
        )
        _startup_sync_thread.start()


def _run_history_retry_sync():
    try:
        result = _serialized_simplefin_sync("history retry", wait=True)
        if result.get("status") not in {"success", "in_progress"}:
            logger.error("Queued history retry did not complete: %s", result.get("message", "unknown error"))
    except Exception as exc:
        logger.exception("Queued account-history retry failed: %s", exc)


def _start_history_retry_sync():
    global _history_retry_thread
    with _history_retry_lock:
        if _history_retry_thread and _history_retry_thread.is_alive():
            return
        _history_retry_thread = threading.Thread(
            target=_run_history_retry_sync,
            name="finance-simplefin-history-retry",
            daemon=True,
        )
        _history_retry_thread.start()


def _run_periodic_sync_once():
    try:
        result = _serialized_simplefin_sync("scheduled")
        if result.get("status") == "in_progress":
            return
        if result.get("status") != "success":
            logger.error("Scheduled SimpleFIN sync failed: %s", result.get("message", "unknown error"))
    except Exception as exc:
        logger.exception("Scheduled SimpleFIN sync raised an exception: %s", exc)


def _periodic_sync_worker():
    while not _periodic_sync_stop.wait(simplefin.SYNC_INTERVAL_SECONDS):
        _run_periodic_sync_once()


def _start_periodic_sync():
    global _periodic_sync_thread
    with _periodic_sync_lock:
        if _periodic_sync_thread and _periodic_sync_thread.is_alive():
            return
        _periodic_sync_stop.clear()
        _periodic_sync_thread = threading.Thread(
            target=_periodic_sync_worker,
            name="finance-simplefin-periodic-sync",
            daemon=True,
        )
        _periodic_sync_thread.start()
        logger.info("Scheduled SimpleFIN sync every %d hours.", simplefin.SYNC_INTERVAL_SECONDS // 3600)


def _stop_periodic_sync():
    _periodic_sync_stop.set()

class ClaimTokenRequest(BaseModel):
    setup_token: str = Field(min_length=1, max_length=4096)


class CategoryRulesRequest(BaseModel):
    rules_json: str = Field(max_length=50000)


class TransactionCategoryRequest(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    apply_to_future_matches: bool = False
    apply_to_existing_matches: bool = False
    rule_match_source: Literal["merchant", "description", "both"] = "merchant"


class AccountTypeRequest(BaseModel):
    account_type: str = Field(min_length=1, max_length=40)


class AccountNicknameRequest(BaseModel):
    nickname: str = Field(max_length=80)


class ManualAssetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=50)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class FinancialGoalRequest(BaseModel):
    goal_type: Literal["debt_payoff", "savings", "retirement"]
    name: str = Field(min_length=1, max_length=80)
    account_ids: list[str] = Field(default_factory=list)
    manual_asset_ids: list[str] = Field(default_factory=list)
    target_amount: str | None = Field(default=None, max_length=50)
    target_date: str | None = Field(default=None, max_length=10)
    apr_percent: str | None = Field(default=None, max_length=12)
    planned_monthly: str | None = Field(default=None, max_length=50)
    retirement_timing_mode: Literal["age", "year"] | None = None
    birth_year: int | None = None
    target_retirement_age: int | None = None
    retirement_year: int | None = None
    expected_return_percent: str | None = Field(default=None, max_length=12)
    inflation_percent: str | None = Field(default=None, max_length=12)
    monthly_income_target: str | None = Field(default=None, max_length=50)
    withdrawal_rate_percent: str | None = Field(default=None, max_length=12)


class DebtPayoffTermRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=256)
    apr_percent: str | None = Field(default=None, max_length=20)
    minimum_payment: str | None = Field(default=None, max_length=50)


class DebtPayoffSettingsRequest(BaseModel):
    funding_account_id: str = Field(min_length=1, max_length=256)
    current_target_account_id: str = Field(min_length=1, max_length=256)
    strategy: Literal["highest_balance", "lowest_balance", "highest_apr", "manual"] = "highest_balance"
    monthly_budget_override: str | None = Field(default=None, max_length=50)
    included_account_ids: list[str] = Field(default_factory=list, max_length=100)
    debt_terms: list[DebtPayoffTermRequest] = Field(default_factory=list, max_length=100)
    manual_order: list[str] = Field(default_factory=list, max_length=100)


class DebtPayoffEstimateRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=256)


class AIChatMessageRequest(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class AIChatRequest(BaseModel):
    preview_id: str = Field(min_length=32, max_length=64)


class AIChatPreviewRequest(BaseModel):
    messages: list[AIChatMessageRequest] = Field(min_length=1, max_length=20)


class PinUnlockRequest(BaseModel):
    pin: str = Field(min_length=4, max_length=64)


class PinSettingsRequest(BaseModel):
    enabled: bool
    idle_timeout_minutes: int = pin_auth.DEFAULT_IDLE_TIMEOUT_MINUTES
    session_timeout_minutes: int = pin_auth.DEFAULT_SESSION_TIMEOUT_MINUTES
    new_pin: str | None = Field(default=None, max_length=64)
    confirm_pin: str | None = Field(default=None, max_length=64)


class CategoryDefaultsRequest(BaseModel):
    top: int = 25
    min_count: int = 2


class RecurringRequest(BaseModel):
    top: int = 40
    min_count: int = 2


class RecurringRulesRequest(BaseModel):
    recurring_rules_json: str = Field(max_length=50000)


class AiSettingsRequest(BaseModel):
    enabled: bool
    provider: str
    model: str
    openai_api_key: str | None = None
    claude_api_key: str | None = None
    gemini_api_key: str | None = None


class AiModelsRequest(BaseModel):
    provider: str
    api_key: str | None = Field(default=None, max_length=4096)


class AiPromptRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=12000)
    reset_to_default: bool = False


class AppLogSettingsRequest(BaseModel):
    level: Literal["INFO", "DEBUG"]


class PlanningSettingsRequest(BaseModel):
    birth_year: int | None = None
    expected_return_percent: str = Field(min_length=1, max_length=12)
    withdrawal_rate_percent: str = Field(min_length=1, max_length=12)
    inflation_percent: str = Field(min_length=1, max_length=12)
    inflation_source: Literal["manual", "bls", "default"] = "manual"
    inflation_window_years: int = 10


class InflationSyncRequest(BaseModel):
    window_years: Literal[5, 10, 20] = 10


class DataHistoryRetryRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=256)
    start_ts: int = Field(ge=0)
    end_ts: int = Field(ge=1)


@app.on_event("startup")
def startup_event():
    logger.info("Initializing database...")
    init_db()
    apply_app_log_level()
    logger.info("Starting initial SimpleFIN sync in the background; HTTP startup will not wait for it.")
    _start_periodic_sync()
    _start_startup_sync()
    if os.environ.get("FINANCE_AGENT_STDIN_ENABLED") == "1":
        threading.Thread(target=_finance_agent_stdin_worker, name="finance-agent-stdin", daemon=True).start()


@app.on_event("shutdown")
def shutdown_event():
    _stop_periodic_sync()


def verify_ha_auth(finance_session: str | None = Header(None, alias="X-Finance-Session")):
    """Require a valid finance session token when PIN protection is enabled."""
    if not pin_auth.pin_is_required():
        return

    if not pin_auth.validate_session_token(finance_session):
        raise HTTPException(status_code=401, detail="PIN required")


def validate_category_rules_json(raw: str):
    try:
        rules = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Rules must be valid JSON.") from exc
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Rules must be an array with at most 100 entries.")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule {index + 1} must be an object.")
        category = rule.get("category")
        patterns = rule.get("patterns")
        if not isinstance(category, str) or not category.strip() or len(category) > 100:
            raise ValueError(f"Rule {index + 1} needs a category of 1–100 characters.")
        if not isinstance(patterns, list) or len(patterns) > 50:
            raise ValueError(f"Rule {index + 1} patterns must be an array of at most 50 entries.")
        if any(not isinstance(pattern, str) or len(pattern) > 200 for pattern in patterns):
            raise ValueError(f"Rule {index + 1} contains an invalid pattern.")
        if "enabled" in rule and not isinstance(rule["enabled"], bool):
            raise ValueError(f"Rule {index + 1} enabled flag must be a boolean.")


def validate_recurring_rules_json(raw: str):
    try:
        rules = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Recurring rules must be valid JSON.") from exc
    if not isinstance(rules, dict):
        raise ValueError("Recurring rules must be a JSON object.")
    ignored = rules.get("ignored_keys", [])
    overrides = rules.get("overrides", {})
    if not isinstance(ignored, list) or len(ignored) > 1000 or any(not isinstance(key, str) or len(key) > 200 for key in ignored):
        raise ValueError("ignored_keys must contain at most 1000 strings of at most 200 characters.")
    if not isinstance(overrides, dict) or len(overrides) > 1000:
        raise ValueError("overrides must be an object with at most 1000 entries.")
    allowed_kinds = {"subscription", "recurring bill", "recurring income", "recurring payment"}
    for key, override in overrides.items():
        if not isinstance(key, str) or not key or len(key) > 200 or not isinstance(override, dict):
            raise ValueError("Each recurring override needs a valid merchant key and object value.")
        label = override.get("label", "")
        kind = override.get("kind", "")
        if not isinstance(label, str) or len(label) > 100:
            raise ValueError("Recurring labels must be strings of at most 100 characters.")
        if not isinstance(kind, str) or (kind and kind not in allowed_kinds):
            raise ValueError("Recurring kind is not supported.")
        if "hidden" in override and not isinstance(override["hidden"], bool):
            raise ValueError("Recurring hidden flag must be a boolean.")


def _finance_agent_stdin_result(payload: dict):
    if pin_auth.pin_is_required() and not pin_auth.validate_session_token(payload.get("finance_session")):
        return {"ok": False, "error": "A valid Finance PIN session is required."}

    command = payload.get("command")
    if command == "merchant_audit":
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 50)
        if type(offset) is not int or offset < 0 or offset > 10000:
            return {"ok": False, "error": "offset must be an integer between 0 and 10000."}
        if type(limit) is not int or limit < 1 or limit > 100:
            return {"ok": False, "error": "limit must be an integer between 1 and 100."}

        db = SessionLocal()
        try:
            grouped = {}
            for tx in db.query(Transaction).all():
                if tx.pending:
                    continue
                raw_label = (tx.payee or "").strip() or (tx.description or "").strip()
                if not raw_label:
                    continue
                label = re.sub(r"\b(?:ref(?:erence)?|txn|transaction|auth|trace|order)\s*[:#-]?\s*[a-z0-9-]+\b", " ", raw_label, flags=re.IGNORECASE)
                label = re.sub(r"\b\d{4,}\b", " ", label)
                label = re.sub(r"\s+", " ", label).strip(" -:#")[:160]
                if not label:
                    continue
                key = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
                if not key:
                    continue
                entry = grouped.setdefault(key, {"labels": Counter(), "categories": Counter(), "count": 0})
                entry["labels"][label] += 1
                entry["categories"][tx.category or "Uncategorized"] += 1
                entry["count"] += 1

            merchants = [
                {
                    "merchant": entry["labels"].most_common(1)[0][0],
                    "count": entry["count"],
                    "categories": dict(entry["categories"]),
                }
                for entry in grouped.values()
            ]
            merchants.sort(key=lambda item: (-item["count"], item["merchant"].casefold()))
            page = merchants[offset:offset + limit]
            return {
                "ok": True,
                "command": command,
                "offset": offset,
                "limit": limit,
                "total_merchants": len(merchants),
                "merchants": page,
                "current_rules_json": get_category_rules_value() or "" if offset == 0 else None,
            }
        finally:
            db.close()

    if command == "apply_category_rules":
        raw_rules = payload.get("rules_json")
        if isinstance(payload.get("rules"), list):
            raw_rules = json.dumps(payload["rules"], ensure_ascii=False)
        if not isinstance(raw_rules, str) or len(raw_rules) > 50000:
            return {"ok": False, "error": "rules_json must be a string of at most 50000 characters."}
        try:
            validate_category_rules_json(raw_rules)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        merchant_overrides = payload.get("merchant_overrides", [])
        if not isinstance(merchant_overrides, list) or len(merchant_overrides) > 100:
            return {"ok": False, "error": "merchant_overrides must be an array with at most 100 entries."}
        valid_categories = {rule["category"] for rule in json.loads(raw_rules)}
        normalized_overrides = []
        for override in merchant_overrides:
            if not isinstance(override, dict):
                return {"ok": False, "error": "Each merchant override must be an object."}
            merchant = override.get("merchant")
            category = override.get("category")
            if not isinstance(merchant, str) or not merchant.strip() or len(merchant) > 200:
                return {"ok": False, "error": "Each merchant override needs a merchant label of 1–200 characters."}
            if not isinstance(category, str) or category not in valid_categories:
                return {"ok": False, "error": "Merchant override categories must exist in the submitted rules."}
            normalized_overrides.append((re.sub(r"[^a-z0-9]+", " ", merchant.casefold()).strip(), category))

        set_category_rules_value(raw_rules)
        rules = simplefin._load_category_rules()
        db = SessionLocal()
        reclassified = 0
        explicitly_overridden = 0
        try:
            for tx in db.query(Transaction).all():
                merchant_text = re.sub(r"[^a-z0-9]+", " ", f"{tx.payee or ''} {tx.description or ''}".casefold()).strip()
                forced_category = next((category for merchant, category in normalized_overrides if merchant and merchant in merchant_text), None)
                if forced_category:
                    if tx.category != forced_category:
                        tx.category = forced_category
                        explicitly_overridden += 1
                    continue
                if (tx.category or "Uncategorized") != "Uncategorized":
                    continue
                category = simplefin.categorize_transaction(
                    {"payee": tx.payee or "", "description": tx.description or ""},
                    rules=rules,
                )
                if category != "Uncategorized":
                    tx.category = category
                    reclassified += 1
            db.commit()
        except Exception:
            db.rollback()
            logger.error("Finance agent category reclassification failed (%s).", type(sys.exc_info()[1]).__name__)
            return {"ok": False, "error": "Rules were saved, but existing uncategorized transactions could not be reclassified."}
        finally:
            db.close()

        return {
            "ok": True,
            "command": command,
            "saved_rule_count": len(json.loads(raw_rules)),
            "reclassified_uncategorized": reclassified,
            "explicit_merchant_overrides": explicitly_overridden,
        }

    return {"ok": False, "error": "Unknown Finance agent command."}


def _finance_agent_stdin_message(message: bytes):
    if len(message) > FINANCE_STDIN_MAX_BYTES:
        return {"ok": False, "error": "Finance agent command exceeds the size limit."}
    try:
        payload = json.loads(message.decode("utf-8"))
        if isinstance(payload, str):
            payload = json.loads(payload) if payload.lstrip().startswith("{") else {"command": payload.strip()}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Finance agent command must be a JSON object."}
        return _finance_agent_stdin_result(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.error("Finance agent stdin command failed (%s).", type(exc).__name__)
        return {"ok": False, "error": "Finance agent command failed; check add-on logs."}


def _finance_agent_stdin_worker():
    """Accept a narrow, admin-only command protocol over Supervisor app stdin."""
    try:
        stdin_fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError) as exc:
        logger.warning("Finance agent stdin unavailable (%s).", type(exc).__name__)
        return

    logger.info("Finance agent stdin command handler ready.")
    buffer = bytearray()

    def process_message(message: bytes):
        result = _finance_agent_stdin_message(message)
        print("FINANCE_AGENT_RESULT " + json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)

    while True:
        try:
            readable, _, _ = select.select([stdin_fd], [], [], 0.5)
        except Exception as exc:
            logger.warning("Finance agent stdin stopped (%s).", type(exc).__name__)
            return
        if not readable:
            if buffer:
                message = bytes(buffer)
                buffer.clear()
                process_message(message)
            continue

        try:
            chunk = os.read(stdin_fd, 4096)
        except OSError as exc:
            logger.warning("Finance agent stdin read failed (%s).", type(exc).__name__)
            return
        if not chunk:
            if buffer:
                process_message(bytes(buffer))
            return

        buffer.extend(chunk)
        while b"\n" in buffer:
            message, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if message.strip():
                process_message(bytes(message))
        if len(buffer) > FINANCE_STDIN_MAX_BYTES:
            process_message(bytes(buffer))
            buffer.clear()


@app.get("/api/status", dependencies=[Depends(verify_ha_auth)])
def api_status():
    return simplefin.get_dashboard_status()


@app.get("/api/planning/settings", dependencies=[Depends(verify_ha_auth)])
def api_get_planning_settings():
    return get_planning_settings()


@app.post("/api/planning/settings", dependencies=[Depends(verify_ha_auth)])
def api_set_planning_settings(payload: PlanningSettingsRequest):
    try:
        return {"status": "success", **save_planning_settings(payload.dict())}
    except PlanningSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/planning/inflation/sync", dependencies=[Depends(verify_ha_auth)])
def api_sync_planning_inflation(payload: InflationSyncRequest):
    try:
        return {"status": "success", **sync_bls_inflation(payload.window_years)}
    except PlanningSettingsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("BLS inflation sync failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not retrieve CPI data from BLS. Your saved settings were not changed.") from exc


@app.get("/api/pin/status")
def api_pin_status(finance_session: str | None = Header(None, alias="X-Finance-Session")):
    return pin_auth.get_pin_status(finance_session)


@app.post("/api/pin/unlock")
def api_pin_unlock(payload: PinUnlockRequest):
    try:
        result = pin_auth.unlock_pin(payload.pin)
        if not result:
            raise HTTPException(status_code=401, detail="Invalid PIN")
        return {"status": "success", **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to unlock PIN: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/pin/lock", dependencies=[Depends(verify_ha_auth)])
def api_pin_lock():
    pin_auth.clear_session()
    return {"status": "success", "message": "PIN session cleared."}


@app.get("/api/pin/settings", dependencies=[Depends(verify_ha_auth)])
def api_get_pin_settings():
    return pin_auth.get_pin_settings()


@app.get("/api/logging/settings", dependencies=[Depends(verify_ha_auth)])
def api_get_logging_settings():
    return get_app_log_settings()


@app.post("/api/logging/settings", dependencies=[Depends(verify_ha_auth)])
def api_set_logging_settings(payload: AppLogSettingsRequest):
    try:
        apply_app_log_level(payload.level)
        return {"status": "success", "message": "Log verbosity saved.", **get_app_log_settings()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/pin/settings", dependencies=[Depends(verify_ha_auth)])
def api_set_pin_settings(payload: PinSettingsRequest, finance_session: str | None = Header(None, alias="X-Finance-Session")):
    try:
        if payload.new_pin is not None and payload.confirm_pin is not None and payload.new_pin != payload.confirm_pin:
            raise HTTPException(status_code=400, detail="PIN confirmation does not match.")

        settings = pin_auth.update_pin_settings(
            enabled=payload.enabled,
            idle_timeout_minutes=payload.idle_timeout_minutes,
            session_timeout_minutes=payload.session_timeout_minutes,
            new_pin=payload.new_pin,
        )
        current_status = pin_auth.get_pin_status(finance_session)
        return {"status": "success", "message": "PIN settings saved.", **settings, **current_status}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save PIN settings: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/accounts", dependencies=[Depends(verify_ha_auth)])
def api_get_accounts():
    """Retrieve financial accounts from primary database."""
    try:
        history = simplefin.get_account_data_history()
        history_by_id = {item["account_id"]: item for item in history["accounts"]}
        accounts = [
            {**account, "data_history": history_by_id.get(str(account.get("id")))}
            for account in get_stored_accounts()
        ]
        return {"accounts": accounts}
    except Exception as e:
        logger.error(f"Failed to query accounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/data-history", dependencies=[Depends(verify_ha_auth)])
def api_get_data_history():
    try:
        return simplefin.get_account_data_history()
    except Exception as exc:
        logger.error("Failed to read account data-history coverage (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not load account history coverage.") from exc


@app.post("/api/data-history/retry", dependencies=[Depends(verify_ha_auth)])
def api_retry_data_history(payload: DataHistoryRetryRequest):
    try:
        queued = simplefin.queue_account_history_retry(payload.account_id, payload.start_ts, payload.end_ts)
        _start_history_retry_sync()
        return {
            "status": "queued",
            "message": "The selected missing range has been queued. It will respect the SimpleFIN request budget.",
            **queued,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Could not queue an account history retry (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not queue the requested history retry.") from exc


@app.get("/api/manual-assets", dependencies=[Depends(verify_ha_auth)])
def api_get_manual_assets():
    try:
        return {"assets": get_manual_assets()}
    except Exception as exc:
        logger.error("Failed to query manual assets (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not load manual assets.") from exc


@app.get("/api/goals", dependencies=[Depends(verify_ha_auth)])
def api_get_goals():
    try:
        return {"goals": get_goals(get_stored_accounts(), get_manual_assets())}
    except Exception as exc:
        logger.error("Failed to query financial goals (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not load financial goals.") from exc


@app.get("/api/debt-payoff", dependencies=[Depends(verify_ha_auth)])
def api_get_debt_payoff_plan():
    try:
        return get_debt_payoff_plan(get_stored_accounts(), simplefin.get_account_data_history())
    except Exception as exc:
        logger.error("Failed to calculate debt payoff plan (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not load the debt payoff plan.") from exc


@app.put("/api/debt-payoff", dependencies=[Depends(verify_ha_auth)])
def api_save_debt_payoff_plan(payload: DebtPayoffSettingsRequest):
    try:
        save_debt_payoff_settings(payload.dict(), get_stored_accounts())
        return get_debt_payoff_plan(get_stored_accounts(), simplefin.get_account_data_history())
    except DebtPayoffValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Failed to save debt payoff settings (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not save the debt payoff settings.") from exc


@app.post("/api/debt-payoff/estimate", dependencies=[Depends(verify_ha_auth)])
def api_estimate_debt_terms(payload: DebtPayoffEstimateRequest):
    try:
        account = next(
            (item for item in get_stored_accounts() if str(item.get("id")) == payload.account_id),
            None,
        )
        if not account:
            raise HTTPException(status_code=404, detail="Liability account not found.")
        return estimate_debt_terms_with_ai(account)
    except DebtPayoffEstimateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Debt payoff estimate failed safely (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not estimate debt terms. No planner fields were changed.") from exc


@app.post("/api/goals", dependencies=[Depends(verify_ha_auth)])
def api_create_goal(payload: FinancialGoalRequest):
    try:
        goal = create_goal(payload.dict(), get_stored_accounts(), get_manual_assets())
        return {"status": "success", "goal": goal}
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Failed to create financial goal (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not save the financial goal.") from exc


@app.put("/api/goals/{goal_id}", dependencies=[Depends(verify_ha_auth)])
def api_update_goal(goal_id: str, payload: FinancialGoalRequest):
    try:
        goal = update_goal(goal_id, payload.dict(), get_stored_accounts(), get_manual_assets())
        if not goal:
            raise HTTPException(status_code=404, detail="Financial goal not found.")
        return {"status": "success", "goal": goal}
    except GoalValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update financial goal (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not update the financial goal.") from exc


@app.delete("/api/goals/{goal_id}", dependencies=[Depends(verify_ha_auth)])
def api_delete_goal(goal_id: str):
    try:
        if not delete_goal(goal_id):
            raise HTTPException(status_code=404, detail="Financial goal not found.")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to delete financial goal (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not delete the financial goal.") from exc


@app.post("/api/manual-assets", dependencies=[Depends(verify_ha_auth)])
def api_create_manual_asset(payload: ManualAssetRequest):
    try:
        asset = create_manual_asset(payload.name, payload.value, payload.currency)
        return {"status": "success", "asset": asset}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Failed to create manual asset (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not create the manual asset.") from exc


@app.put("/api/manual-assets/{asset_id}", dependencies=[Depends(verify_ha_auth)])
def api_update_manual_asset(asset_id: str, payload: ManualAssetRequest):
    try:
        asset = update_manual_asset(asset_id, payload.name, payload.value, payload.currency)
        if not asset:
            raise HTTPException(status_code=404, detail="Manual asset not found")
        return {"status": "success", "asset": asset}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update manual asset (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not update the manual asset.") from exc


@app.delete("/api/manual-assets/{asset_id}", dependencies=[Depends(verify_ha_auth)])
def api_delete_manual_asset(asset_id: str):
    try:
        if not delete_manual_asset(asset_id):
            raise HTTPException(status_code=404, detail="Manual asset not found")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to delete manual asset (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not remove the manual asset.") from exc


@app.put("/api/accounts/{account_id}/type", dependencies=[Depends(verify_ha_auth)])
def api_set_account_type(account_id: str, payload: AccountTypeRequest):
    try:
        account = set_account_type(account_id, payload.account_type)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return {"status": "success", "account": account}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update account type (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not update the account type.") from exc


@app.put("/api/accounts/{account_id}/nickname", dependencies=[Depends(verify_ha_auth)])
def api_set_account_nickname(account_id: str, payload: AccountNicknameRequest):
    try:
        account = set_account_nickname(account_id, payload.nickname)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found")
        return {"status": "success", "account": account}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to update account nickname (%s).", type(exc).__name__)
        raise HTTPException(status_code=400, detail="Could not update the account nickname.") from exc


@app.get("/api/transactions", dependencies=[Depends(verify_ha_auth)])
def api_get_transactions(limit: int = Query(default=500, ge=1, le=500), offset: int = Query(default=0, ge=0)):
    """Retrieve financial transactions from primary database."""
    try:
        return {
            "transactions": get_stored_transactions(limit=limit, offset=offset),
            "total": get_transaction_count(),
            "limit": limit,
            "offset": offset,
        }
    except Exception as e:
        logger.error(f"Failed to query transactions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/simplefin/claim-token", dependencies=[Depends(verify_ha_auth)])
def api_claim_token(payload: ClaimTokenRequest):
    """Claim a SimpleFIN setup token and exchange it for a persistent access token."""
    try:
        result = simplefin.claim_simplefin_setup_token(payload.setup_token)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("message"))

        sync_result = _serialized_simplefin_sync("claim", wait=True)
        if sync_result.get("status") == "in_progress":
            return {
                "status": "in_progress",
                "message": "Token claimed. Another SimpleFIN sync is already running; data will update when it finishes.",
                "sync_result": sync_result,
            }
        return {
            "status": "success",
            "message": "Token claimed and initial sync performed",
            "sync_result": sync_result,
        }
    except Exception as e:
        logger.error(f"Failed to claim token API error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/sync", dependencies=[Depends(verify_ha_auth)])
def api_sync():
    """Trigger an out-of-band sync from SimpleFIN upstream."""
    return _serialized_simplefin_sync("manual")


@app.get("/api/category-rules", dependencies=[Depends(verify_ha_auth)])
def api_get_category_rules():
    return {"rules_json": get_category_rules_value() or ""}


@app.post("/api/category-rules", dependencies=[Depends(verify_ha_auth)])
def api_set_category_rules(payload: CategoryRulesRequest):
    try:
        validate_category_rules_json(payload.rules_json)
        set_category_rules_value(payload.rules_json)
        return {"status": "success", "message": "Category rules saved."}
    except Exception as e:
        logger.error(f"Failed to save category rules: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/category-defaults", dependencies=[Depends(verify_ha_auth)])
def api_get_category_defaults(top: int = Query(default=25, ge=1, le=100), min_count: int = Query(default=2, ge=1, le=10000)):
    try:
        analysis = generate_category_analysis(top_n=top, min_count=min_count)
        return {"status": "success", **analysis}
    except Exception as e:
        logger.error(f"Failed to generate category defaults: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/ai/settings", dependencies=[Depends(verify_ha_auth)])
def api_get_ai_settings():
    return get_ai_settings()


@app.post("/api/ai/models", dependencies=[Depends(verify_ha_auth)])
def api_list_ai_models(payload: AiModelsRequest):
    try:
        return {
            "status": "success",
            "provider": payload.provider.strip().lower(),
            "models": list_provider_models(payload.provider, payload.api_key),
        }
    except ProviderModelDiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("AI model discovery failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not retrieve models from the selected provider.") from exc


@app.get("/api/ai/prompts", dependencies=[Depends(verify_ha_auth)])
def api_get_ai_prompts():
    return get_ai_prompt_settings()


@app.post("/api/ai/prompts/{prompt_id}", dependencies=[Depends(verify_ha_auth)])
def api_set_ai_prompt(prompt_id: str, payload: AiPromptRequest):
    try:
        if payload.reset_to_default and payload.prompt is not None:
            raise AIPromptValidationError("Choose either a custom prompt or Reset to default.")
        prompt = save_ai_system_prompt(
            prompt_id,
            payload.prompt,
            reset_to_default=payload.reset_to_default,
        )
        return {"status": "success", "prompt_id": prompt_id, **prompt}
    except AIPromptValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/ai/settings", dependencies=[Depends(verify_ha_auth)])
def api_set_ai_settings(payload: AiSettingsRequest):
    try:
        settings = update_ai_settings(
            enabled=payload.enabled,
            provider=payload.provider,
            model=payload.model,
            openai_api_key=payload.openai_api_key,
            claude_api_key=payload.claude_api_key,
            gemini_api_key=payload.gemini_api_key,
        )
        return {"status": "success", "message": "AI settings saved.", **settings}
    except Exception as e:
        logger.error(f"Failed to save AI settings: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/ai/chat/preview", dependencies=[Depends(verify_ha_auth)])
def api_preview_ai_chat_message(payload: AIChatPreviewRequest):
    try:
        return prepare_goal_chat_preview([message.dict() for message in payload.messages])
    except AIChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("AI chat privacy preview failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Could not prepare a sanitized message.") from exc


@app.post("/api/ai/chat", dependencies=[Depends(verify_ha_auth)])
def api_ai_chat(payload: AIChatRequest):
    try:
        return create_goal_chat_response(payload.preview_id)
    except AIProviderRequestError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except AIChatError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("AI goal chat failed (%s).", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not complete the chat request. No chat content was logged.") from exc


@app.get("/api/category-defaults/ai", dependencies=[Depends(verify_ha_auth)])
def api_get_category_defaults_ai(top: int = Query(default=25, ge=1, le=100), min_count: int = Query(default=2, ge=1, le=10000)):
    try:
        analysis = build_ai_category_defaults(top_n=top, min_count=min_count)
        return {"status": "success", **analysis}
    except Exception as e:
        logger.error("Failed to generate AI category defaults (%s).", type(e).__name__)
        active_key = _provider_key(get_ai_settings()["provider"])
        raise HTTPException(status_code=400, detail=redact_api_keys(str(e), active_key))


@app.get("/api/recurring-payments", dependencies=[Depends(verify_ha_auth)])
def api_get_recurring_payments(top: int = Query(default=40, ge=1, le=100), min_count: int = Query(default=2, ge=2, le=10000)):
    try:
        analysis = generate_recurring_analysis(top_n=top, min_count=min_count)
        return {"status": "success", **analysis}
    except Exception as e:
        logger.error(f"Failed to generate recurring analysis: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/recurring-rules", dependencies=[Depends(verify_ha_auth)])
def api_get_recurring_rules():
    return {"recurring_rules_json": get_recurring_rules_value() or ""}


@app.post("/api/recurring-rules", dependencies=[Depends(verify_ha_auth)])
def api_set_recurring_rules(payload: RecurringRulesRequest):
    try:
        validate_recurring_rules_json(payload.recurring_rules_json)
        set_recurring_rules_value(payload.recurring_rules_json)
        return {"status": "success", "message": "Recurring payment rules saved."}
    except Exception as e:
        logger.error(f"Failed to save recurring rules: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/transactions/{transaction_id}/category", dependencies=[Depends(verify_ha_auth)])
def api_set_transaction_category(transaction_id: str, payload: TransactionCategoryRequest):
    try:
        ok = set_transaction_category(
            transaction_id,
            payload.category,
            apply_to_future_matches=payload.apply_to_future_matches,
            apply_to_existing_matches=payload.apply_to_existing_matches,
            rule_match_source=payload.rule_match_source,
            default_category_rules=simplefin.DEFAULT_CATEGORY_RULES,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Transaction not found")
        updated_existing_count = ok["updated_existing_count"]
        if payload.apply_to_existing_matches:
            existing_label = "transaction" if updated_existing_count == 1 else "transactions"
            message = f"Future-match rule saved; updated {updated_existing_count} existing matching {existing_label}."
        elif payload.apply_to_future_matches:
            message = "Transaction category updated and future-match rule saved."
        else:
            message = "Transaction category updated."
        return {
            "status": "success",
            "message": message,
            "updated_existing_count": updated_existing_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        if isinstance(e, ValueError):
            raise HTTPException(status_code=400, detail=str(e)) from e
        logger.error("Failed to update transaction category (%s).", type(e).__name__)
        raise HTTPException(status_code=400, detail="Failed to update the transaction category.") from e


# Mount frontend static files.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
