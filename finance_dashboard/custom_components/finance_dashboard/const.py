"""Constants for the Finance Dashboard integration."""

from datetime import timedelta

DOMAIN = "finance_dashboard"
PLATFORMS = ["sensor"]
DEFAULT_URL = "http://custom_finance:8000"
DEFAULT_SCAN_INTERVAL = timedelta(minutes=15)
CONF_EXPOSE_TRANSACTIONS = "expose_transactions"
API_PATH = "/api/ha-metrics"
