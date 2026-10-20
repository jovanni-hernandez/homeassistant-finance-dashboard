from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import simplefin
from database import init_db, get_stored_accounts, get_stored_transactions

app = FastAPI(title="Finance Dashboard API")

# Enable CORS for local Vite/UI development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup_event():
    init_db()
    # Initial sync or seed on startup
    simplefin.fetch_and_sync_simplefin()

def verify_ha_auth(authorization: str = Header(None)):
    """
    Middleware to verify the Home Assistant access token.
    """
    pass

@app.get("/api/accounts", dependencies=[Depends(verify_ha_auth)])
def api_get_accounts():
    """Retrieve financial accounts from primary database."""
    return {"accounts": get_stored_accounts()}

@app.get("/api/transactions", dependencies=[Depends(verify_ha_auth)])
def api_get_transactions(limit: int = 100):
    """Retrieve financial transactions from primary database."""
    return {"transactions": get_stored_transactions(limit=limit)}

@app.post("/api/sync", dependencies=[Depends(verify_ha_auth)])
def api_sync():
    """Trigger an out-of-band sync from SimpleFIN upstream."""
    result = simplefin.fetch_and_sync_simplefin()
    return result

# Mount frontend static files. This allows the backend to serve the UI directly.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
