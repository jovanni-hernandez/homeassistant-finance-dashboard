"""HTTP client for the Finance Dashboard metrics endpoint."""

from __future__ import annotations

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import API_PATH


class FinanceDashboardApiError(Exception):
    """Raised when the metrics API returns an invalid or failing response."""


class FinanceDashboardAuthError(FinanceDashboardApiError):
    """Raised when the metrics API token is missing or invalid."""


async def async_get_metrics(
    hass: HomeAssistant,
    base_url: str,
    token: str,
    *,
    include_transactions: bool = False,
) -> dict:
    """Fetch the bounded metrics snapshot."""
    url = f"{base_url.rstrip('/')}{API_PATH}"
    headers = {"X-Finance-Metrics-Token": token} if token else {}
    params = {"include_transactions": "true"} if include_transactions else None
    session = async_get_clientsession(hass)

    try:
        async with session.get(
            url,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status == 401:
                raise FinanceDashboardAuthError("The metrics API token was rejected.")
            response.raise_for_status()
            payload = await response.json()
    except FinanceDashboardAuthError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise FinanceDashboardApiError("Could not retrieve Finance Dashboard metrics.") from err

    if not isinstance(payload, dict):
        raise FinanceDashboardApiError("The metrics API returned an invalid response.")
    return payload
