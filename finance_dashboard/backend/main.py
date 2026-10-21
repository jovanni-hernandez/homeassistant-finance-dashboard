import logging
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import simplefin
from ai_assistant import build_ai_category_defaults, get_ai_settings, update_ai_settings
from category_analysis import generate_category_analysis
from recurring_analysis import generate_recurring_analysis
from database import (
    init_db,
    get_stored_accounts,
    get_stored_transactions,
    get_category_rules_value,
    set_category_rules_value,
    set_transaction_category,
)
import pin_auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finance_dashboard")

app = FastAPI(title="Finance Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ClaimTokenRequest(BaseModel):
    setup_token: str


class CategoryRulesRequest(BaseModel):
    rules_json: str


class TransactionCategoryRequest(BaseModel):
    category: str


class PinUnlockRequest(BaseModel):
    pin: str


class PinSettingsRequest(BaseModel):
    enabled: bool
    idle_timeout_minutes: int = pin_auth.DEFAULT_IDLE_TIMEOUT_MINUTES
    session_timeout_minutes: int = pin_auth.DEFAULT_SESSION_TIMEOUT_MINUTES
    new_pin: str | None = None
    confirm_pin: str | None = None


class CategoryDefaultsRequest(BaseModel):
    top: int = 25
    min_count: int = 2


class RecurringRequest(BaseModel):
    top: int = 40
    min_count: int = 2


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


def verify_ha_auth(finance_session: str | None = Header(None, alias="X-Finance-Session")):
    """Require a valid finance session token when PIN protection is enabled."""
    if not pin_auth.pin_is_required():
        return

    if not pin_auth.validate_session_token(finance_session):
        raise HTTPException(status_code=401, detail="PIN required")


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
def api_get_transactions(limit: int = 100):
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
        set_category_rules_value(payload.rules_json)
        return {"status": "success", "message": "Category rules saved."}
    except Exception as e:
        logger.error(f"Failed to save category rules: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/category-defaults", dependencies=[Depends(verify_ha_auth)])
def api_get_category_defaults(top: int = 25, min_count: int = 2):
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
def api_get_category_defaults_ai(top: int = 25, min_count: int = 2):
    try:
        analysis = build_ai_category_defaults(top_n=top, min_count=min_count)
        return {"status": "success", **analysis}
    except Exception as e:
        logger.error(f"Failed to generate AI category defaults: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/recurring-payments", dependencies=[Depends(verify_ha_auth)])
def api_get_recurring_payments(top: int = 40, min_count: int = 2):
    try:
        analysis = generate_recurring_analysis(top_n=top, min_count=min_count)
        return {"status": "success", **analysis}
    except Exception as e:
        logger.error(f"Failed to generate recurring analysis: {e}")
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
