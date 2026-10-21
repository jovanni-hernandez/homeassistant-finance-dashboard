import logging
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import simplefin
from database import (
    init_db,
    get_stored_accounts,
    get_stored_transactions,
    get_category_rules_value,
    set_category_rules_value,
    set_transaction_category,
)

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


@app.on_event("startup")
def startup_event():
    logger.info("Initializing database...")
    init_db()
    logger.info("Syncing / seeding initial data...")
    try:
        simplefin.fetch_and_sync_simplefin()
    except Exception as e:
        logger.error(f"Startup sync failed: {e}")


def verify_ha_auth(authorization: str = Header(None)):
    """Placeholder for Home Assistant auth token verification."""
    pass


@app.get("/api/status", dependencies=[Depends(verify_ha_auth)])
def api_status():
    return simplefin.get_dashboard_status()


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
