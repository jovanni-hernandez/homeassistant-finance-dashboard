import base64
import requests
import json
import logging
from database import save_simplefin_data

logger = logging.getLogger("finance_dashboard")

OPTIONS_PATH = "/data/options.json"

def get_simplefin_token():
    try:
        with open(OPTIONS_PATH) as f:
            options = json.load(f)
            return options.get("simplefin_token")
    except Exception:
        return None

def fetch_and_sync_simplefin():
    """Fetch normalized financial data from SimpleFIN and persist to DB."""
    token = get_simplefin_token()
    
    if token and token != "":
        try:
            decoded_url = base64.b64decode(token).decode('utf-8')
            response = requests.get(f"{decoded_url}/accounts", timeout=30)
            response.raise_for_status()
            data = response.json()
            save_simplefin_data(data)
            return {"status": "success", "message": "Synced successfully from SimpleFIN"}
        except Exception as e:
            logger.error(f"Error fetching SimpleFIN data: {e}")
            return {"status": "error", "message": str(e)}
    
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
