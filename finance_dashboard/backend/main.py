import logging
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import simplefin
from database import init_db, get_stored_accounts, get_stored_transactions

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

@app.post("/api/sync", dependencies=[Depends(verify_ha_auth)])
def api_sync():
    """Trigger an out-of-band sync from SimpleFIN upstream."""
    result = simplefin.fetch_and_sync_simplefin()
    return result

# Mount frontend static files.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
