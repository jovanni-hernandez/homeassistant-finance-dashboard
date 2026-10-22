import logging
import json
import re
import sys
import threading
from collections import Counter, defaultdict
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import simplefin
from ai_assistant import build_ai_category_defaults, get_ai_settings, update_ai_settings
from category_analysis import generate_category_analysis
from recurring_analysis import generate_recurring_analysis
from database import (
    init_db,
    get_stored_accounts,
    get_stored_transactions,
    get_category_rules_value,
    get_recurring_rules_value,
    set_category_rules_value,
    set_recurring_rules_value,
    set_transaction_category,
)
import pin_auth
from models import SessionLocal, Transaction

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finance_dashboard")

app = FastAPI(title="Finance Dashboard API")

FINANCE_STDIN_MAX_BYTES = 65536
FINANCE_AUDIT_PAGE_SIZE = 100

class ClaimTokenRequest(BaseModel):
    setup_token: str = Field(min_length=1, max_length=4096)


class CategoryRulesRequest(BaseModel):
    rules_json: str = Field(max_length=50000)


class TransactionCategoryRequest(BaseModel):
    category: str = Field(min_length=1, max_length=100)


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


@app.on_event("startup")
def startup_event():
    logger.info("Initializing database...")
    init_db()
    logger.info("Syncing / seeding initial data...")
    try:
        simplefin.fetch_and_sync_simplefin()
    except Exception as e:
        logger.error(f"Startup sync failed: {e}")
    threading.Thread(target=_finance_agent_stdin_worker, name="finance-agent-stdin", daemon=True).start()


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

        set_category_rules_value(raw_rules)
        rules = simplefin._load_category_rules()
        db = SessionLocal()
        reclassified = 0
        try:
            for tx in db.query(Transaction).all():
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
        }

    return {"ok": False, "error": "Unknown Finance agent command."}


def _finance_agent_stdin_worker():
    """Accept a narrow, admin-only command protocol over Supervisor app stdin."""
    while True:
        try:
            line = sys.stdin.readline(FINANCE_STDIN_MAX_BYTES + 1)
        except Exception as exc:
            logger.warning("Finance agent stdin stopped (%s).", type(exc).__name__)
            return
        if not line:
            return
        if len(line.encode("utf-8", errors="replace")) > FINANCE_STDIN_MAX_BYTES:
            result = {"ok": False, "error": "Finance agent command exceeds the size limit."}
        else:
            try:
                payload = json.loads(line)
                if isinstance(payload, str):
                    payload = json.loads(payload) if payload.lstrip().startswith("{") else {"command": payload.strip()}
                if not isinstance(payload, dict):
                    result = {"ok": False, "error": "Finance agent command must be a JSON object."}
                else:
                    result = _finance_agent_stdin_result(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                result = {"ok": False, "error": str(exc)}
            except Exception as exc:
                logger.error("Finance agent stdin command failed (%s).", type(exc).__name__)
                result = {"ok": False, "error": "Finance agent command failed; check add-on logs."}
        print("FINANCE_AGENT_RESULT " + json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)


@app.get("/api/status", dependencies=[Depends(verify_ha_auth)])
def api_status():
    return simplefin.get_dashboard_status()


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
        return {"accounts": get_stored_accounts()}
    except Exception as e:
        logger.error(f"Failed to query accounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/transactions", dependencies=[Depends(verify_ha_auth)])
def api_get_transactions(limit: int = Query(default=100, ge=1, le=500)):
    """Retrieve financial transactions from primary database."""
    try:
        return {"transactions": get_stored_transactions(limit=limit)}
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

        sync_result = simplefin.fetch_and_sync_simplefin()
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
    result = simplefin.fetch_and_sync_simplefin()
    return result


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


@app.get("/api/category-defaults/ai", dependencies=[Depends(verify_ha_auth)])
def api_get_category_defaults_ai(top: int = Query(default=25, ge=1, le=100), min_count: int = Query(default=2, ge=1, le=10000)):
    try:
        analysis = build_ai_category_defaults(top_n=top, min_count=min_count)
        return {"status": "success", **analysis}
    except Exception as e:
        logger.error(f"Failed to generate AI category defaults: {e}")
        raise HTTPException(status_code=400, detail=str(e))


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
        ok = set_transaction_category(transaction_id, payload.category)
        if not ok:
            raise HTTPException(status_code=404, detail="Transaction not found")
        return {"status": "success", "message": "Transaction category updated."}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update transaction category: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# Mount frontend static files.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
