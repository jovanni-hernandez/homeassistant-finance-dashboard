"""Shared polling coordinator for Finance Dashboard sensors."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FinanceDashboardApiError,
    FinanceDashboardAuthError,
    async_get_metrics,
)
from .const import CONF_EXPOSE_TRANSACTIONS, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class FinanceDashboardCoordinator(DataUpdateCoordinator[dict]):
    """Fetch all sensor values in one request every 15 minutes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=DEFAULT_SCAN_INTERVAL,
            always_update=False,
        )
        self.entry = entry

    async def _async_update_data(self) -> dict:
        """Retrieve the latest snapshot."""
        try:
            return await async_get_metrics(
                self.hass,
                self.entry.data["url"],
                self.entry.data.get("password", ""),
                include_transactions=self.entry.options.get(CONF_EXPOSE_TRANSACTIONS, False),
            )
        except FinanceDashboardAuthError as err:
            raise ConfigEntryAuthFailed from err
        except FinanceDashboardApiError as err:
            raise UpdateFailed("Could not update Finance Dashboard sensors.") from err
