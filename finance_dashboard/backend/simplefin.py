import base64
import requests
import json
import logging
from database import save_simplefin_data, get_setting, set_setting

logger = logging.getLogger("finance_dashboard")

OPTIONS_PATH = "/data/options.json"

def get_simplefin_token():
    """Prioritize fetching token from database, then add-on options."""
    # 1. Prioritize fetching token from database settings
    try:
        db_token = get_setting("simplefin_access_token")
        if db_token:
            logger.info("SimpleFIN access token retrieved from DB.")
            return db_token
    except Exception as e:
        logger.warning(f"Could not retrieve SimpleFIN token from DB (initial attempt): {e}")

    # 2. Fall back to Add-on options.json
    try:
        with open(OPTIONS_PATH) as f:
            options = json.load(f)
            token = options.get("simplefin_token")
            if token and token.strip():
                logger.info("SimpleFIN token retrieved from options.json.")
                return token.strip()
    except Exception as e:
        logger.warning(f"Could not retrieve SimpleFIN token from options.json: {e}")
        
    logger.info("SimpleFIN access token not found in DB or options.json.")
    return None

def set_simplefin_token(token: str):
    """Save SimpleFIN access token securely to the database settings."""
    logger.info("Saving SimpleFIN access token to DB...")
    set_setting("simplefin_access_token", token.strip())
    return True

def claim_simplefin_setup_token(setup_token: str):
    """
    Claim a SimpleFIN setup token. 
    A SimpleFIN setup token is a base64 encoded claim URL. 
    Making a POST request to that claim URL exchanges it for the actual Access Token (also base64 encoded URL).
    """
    try:
        setup_token = setup_token.strip()
        if not setup_token:
            raise ValueError("Setup token cannot be empty.")

        # Decode the claim URL
        claim_url = base64.b64decode(setup_token).decode('utf-8')
        logger.info(f"Attempting to claim SimpleFIN token from: {claim_url}")
        
        # Request access token from claim URL
        response = requests.post(claim_url, timeout=30)
        response.raise_for_status()
        
        # The response body is the raw Access URL
        access_url = response.text.strip()
        logger.info("Successfully received SimpleFIN access URL.")
        
        # Encode the access URL back to base64 to match SimpleFIN token format
        access_token = base64.b64encode(access_url.encode('utf-8')).decode('utf-8')
        
        # Save it
        set_simplefin_token(access_token)
        logger.info("SimpleFIN access token saved.")
        return {"status": "success", "message": "Successfully claimed SimpleFIN token"}
    except Exception as e:
        logger.error(f"Failed to claim SimpleFIN setup token: {e}")
        return {"status": "error", "message": str(e)}

def fetch_and_sync_simplefin():
    """Fetch normalized financial data from SimpleFIN and persist to DB."""
    token = get_simplefin_token()
    
    if token and token != "":
        try:
            decoded_url = base64.b64decode(token).decode('utf-8')
            logger.info(f"Fetching accounts from SimpleFIN API: {decoded_url}/accounts")
            response = requests.get(f"{decoded_url}/accounts", timeout=30)
            response.raise_for_status()
            data = response.json()
            save_simplefin_data(data)
            logger.info("SimpleFIN data synced and saved to DB.")
            return {"status": "success", "message": "Synced successfully from SimpleFIN"}
        except Exception as e:
            logger.error(f"Error fetching SimpleFIN data: {e}")
            return {"status": "error", "message": str(e)}
    
    logger.warning("No SimpleFIN access token found. Seeding mock data.")
    # If no token, seed mock data for development
    mock_data = {
        "accounts": [
            {
                "id": "1", 
                "org": {"name": "Capital One"}, 
                "name": "Venture X", 
                "balance": "1250.00", 
                "currency": "USD",
                "transactions": [
                    {"id": "tx1", "date": 1718500000, "amount": "-45.50", "payee": "Grocery Store", "description": "Weekly groceries", "pending": False},
                    {"id": "tx2", "date": 1718400000, "amount": "-12.00", "payee": "Coffee Shop", "description": "Latte", "pending": False}
                ]
            },
            {
                "id": "2", 
                "org": {"name": "TSP"}, 
                "name": "L Fund 2050", 
                "balance": "125000.00", 
                "currency": "USD",
                "transactions": [
                    {"id": "tx3", "date": 1718000000, "amount": "500.00", "payee": "Payroll Deposit", "description": "Bi-weekly contribution", "pending": False}
                ]
            },
            {
                "id": "3", 
                "org": {"name": "Navy Federal"}, 
                "name": "Checking", 
                "balance": "3400.50", 
                "currency": "USD",
                "transactions": []
            },
            {
                "id": "4", 
                "org": {"name": "Fidelity"}, 
                "name": "Brokerage", 
                "balance": "45200.00", 
                "currency": "USD",
                "transactions": []
            }
        ]
    }
    save_simplefin_data(mock_data)
    return {"status": "success", "message": "Loaded mock data"}
