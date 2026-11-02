"""Sensors provided by the Finance Dashboard integration."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_EXPOSE_TRANSACTIONS, DOMAIN
from .coordinator import FinanceDashboardCoordinator


def _entity_key(value: str) -> str:
    """Create a stable opaque fragment for identifiers derived from account IDs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp into the timezone-aware form HA requires."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _date(value: str | None) -> date | None:
    """Parse the provider's YYYY-MM-DD transaction date."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up static and data-derived sensors."""
    coordinator: FinanceDashboardCoordinator = entry.runtime_data
    added_unique_ids: set[str] = set()

    @callback
    def add_missing_entities() -> None:
        entities = _build_entities(coordinator, entry)
        new_entities = [
            entity for entity in entities if entity.unique_id not in added_unique_ids
        ]
        if not new_entities:
            return
        added_unique_ids.update(entity.unique_id for entity in new_entities)
        async_add_entities(new_entities)

    add_missing_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_missing_entities))


def _build_entities(
    coordinator: FinanceDashboardCoordinator,
    entry: ConfigEntry,
) -> list[SensorEntity]:
    """Create sensors for the currencies and accounts in the current snapshot."""
    entities: list[SensorEntity] = [
        FinanceDashboardSensor(coordinator, entry, "account_count", "Account count"),
        FinanceDashboardSensor(coordinator, entry, "transaction_count", "Transaction count"),
        FinanceDashboardSensor(coordinator, entry, "connected", "Connection status"),
        FinanceDashboardSensor(coordinator, entry, "last_sync_status", "Last sync status"),
        FinanceDashboardSensor(coordinator, entry, "last_sync_at", "Last sync"),
        FinanceDashboardSensor(
            coordinator,
            entry,
            "incoming_transaction_count",
            "Transactions returned by last sync",
        ),
        FinanceDashboardSensor(
            coordinator,
            entry,
            "latest_incoming_transaction_date",
            "Latest transaction returned by last sync",
        ),
        FinanceDashboardSensor(
            coordinator,
            entry,
            "newest_transaction_date_advanced",
            "Latest transaction date advanced",
        ),
    ]

    for totals in coordinator.data.get("net_worth", []):
        currency = totals["currency"]
        for metric, label in (
            ("assets", "assets"),
            ("liabilities", "liabilities"),
            ("net_worth", "net worth"),
        ):
            entities.append(
                FinanceDashboardSensor(
                    coordinator,
                    entry,
                    metric,
                    f"{currency} {label}",
                    currency=currency,
                )
            )

    for totals in coordinator.data.get("cash_flow", []):
        currency = totals["currency"]
        for metric, label in (
            ("income", "income month to date"),
            ("spending", "spending month to date"),
            ("net", "net cash flow month to date"),
        ):
            entities.append(
                FinanceDashboardSensor(
                    coordinator,
                    entry,
                    f"cash_flow_{metric}",
                    f"{currency} {label}",
                    currency=currency,
                )
            )

    for account in coordinator.data.get("accounts", []):
        account_id = str(account["id"])
        account_name = account.get("nickname") or account.get("name") or "Account"
        entities.append(
            FinanceDashboardSensor(
                coordinator,
                entry,
                "account_balance",
                f"{account_name} balance",
                currency=account.get("currency"),
                account_id=account_id,
            )
        )

    if entry.options.get(CONF_EXPOSE_TRANSACTIONS, False):
        entities.append(
            FinanceDashboardSensor(
                coordinator,
                entry,
                "transactions",
                "Recent transactions",
            )
        )
    return entities


class FinanceDashboardSensor(CoordinatorEntity[FinanceDashboardCoordinator], SensorEntity):
    """A Finance Dashboard metric sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FinanceDashboardCoordinator,
        entry: ConfigEntry,
        metric: str,
        name: str,
        *,
        currency: str | None = None,
        account_id: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._metric = metric
        self._currency = currency
        self._account_id = account_id
        self._attr_name = name
        unique_fragment = (
            f"account_{_entity_key(account_id)}_balance"
            if account_id is not None
            else f"{metric}_{currency.lower()}" if currency else metric
        )
        self._attr_unique_id = f"{entry.entry_id}_{unique_fragment}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Finance Dashboard",
            manufacturer="Personal Finance Dashboard",
        )
        if currency:
            self._attr_native_unit_of_measurement = currency
            self._attr_suggested_display_precision = 2
        if metric == "last_sync_at":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        elif metric == "latest_incoming_transaction_date":
            self._attr_device_class = SensorDeviceClass.DATE

    @property
    def native_value(self) -> Any:
        """Return the current metric value."""
        data = self.coordinator.data
        status = data.get("status", {})

        if self._metric == "account_count":
            return status.get("account_count", 0)
        if self._metric == "transaction_count":
            return status.get("transaction_count", 0)
        if self._metric == "connected":
            return "connected" if status.get("connected") else "disconnected"
        if self._metric == "last_sync_status":
            return status.get("last_sync_status", "never")
        if self._metric == "last_sync_at":
            return _timestamp(status.get("last_sync_at"))
        if self._metric == "incoming_transaction_count":
            return status.get("incoming_transaction_count", 0)
        if self._metric == "latest_incoming_transaction_date":
            return _date(status.get("latest_incoming_transaction_date"))
        if self._metric == "newest_transaction_date_advanced":
            advanced = status.get("newest_transaction_date_advanced")
            if advanced is None:
                return "unknown"
            return "advanced" if advanced else "unchanged"
        if self._metric == "account_balance":
            for account in data.get("accounts", []):
                if str(account.get("id")) == self._account_id:
                    return account.get("balance")
            return None
        if self._metric.startswith("cash_flow_"):
            field = self._metric.removeprefix("cash_flow_")
            rows = data.get("cash_flow", [])
        else:
            field = self._metric
            rows = data.get("net_worth", [])
        for row in rows:
            if row.get("currency") == self._currency:
                return row.get(field)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the explicitly enabled, bounded raw transaction snapshot."""
        if self._metric != "transactions":
            return None
        transactions = self.coordinator.data.get("transactions", [])
        total_count = self.coordinator.data.get("status", {}).get("transaction_count", 0)
        return {
            "transactions": transactions,
            "returned_count": len(transactions),
            "truncated": total_count > len(transactions),
            "maximum_returned": 20,
        }
