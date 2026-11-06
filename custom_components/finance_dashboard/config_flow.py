"""Config and options flows for Finance Dashboard."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import probatio

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_URL
from homeassistant.core import HomeAssistant, callback

from .api import (
    FinanceDashboardApiError,
    FinanceDashboardAuthError,
    async_get_metrics,
)
from .const import CONF_EXPOSE_TRANSACTIONS, DEFAULT_URL, DOMAIN


def _normalize_url(value: str) -> str:
    """Validate and normalize the internal add-on URL."""
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Enter an HTTP(S) base URL without credentials, query, or fragment."
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


async def _validate_connection(
    hass: HomeAssistant,
    base_url: str,
    token: str,
) -> str | None:
    """Return a config-flow error key, or None when the endpoint responds."""
    try:
        await async_get_metrics(hass, base_url, token)
    except FinanceDashboardAuthError:
        return "invalid_auth"
    except FinanceDashboardApiError:
        return "cannot_connect"
    return None


class FinanceDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Finance Dashboard sensors."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle setup from the integrations UI."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = _normalize_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                token = str(user_input.get(CONF_PASSWORD) or "").strip()
                error = await _validate_connection(self.hass, base_url, token)
                if error:
                    errors["base"] = error
                else:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="Finance Dashboard",
                        data={CONF_URL: base_url, CONF_PASSWORD: token},
                        options={CONF_EXPOSE_TRANSACTIONS: False},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_URL, default=DEFAULT_URL): str,
                    probatio.Optional(CONF_PASSWORD, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the internal URL or optional token."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = _normalize_url(user_input[CONF_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                token = str(user_input.get(CONF_PASSWORD) or "").strip()
                error = await _validate_connection(self.hass, base_url, token)
                if error:
                    errors["base"] = error
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_URL: base_url, CONF_PASSWORD: token},
                    )

        schema = probatio.Schema(
            {
                probatio.Required(CONF_URL): str,
                probatio.Optional(CONF_PASSWORD, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    CONF_URL: entry.data[CONF_URL],
                    CONF_PASSWORD: entry.data.get(CONF_PASSWORD, ""),
                },
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the configured token is rejected."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_entry_not_found")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a replacement token."""
        entry = self._reauth_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            token = str(user_input.get(CONF_PASSWORD) or "").strip()
            error = await _validate_connection(self.hass, entry.data[CONF_URL], token)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_PASSWORD: token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {probatio.Optional(CONF_PASSWORD, default=""): str}
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FinanceDashboardOptionsFlow:
        """Return the options flow."""
        return FinanceDashboardOptionsFlow()


class FinanceDashboardOptionsFlow(OptionsFlowWithReload):
    """Configure optional raw transaction exposure."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_EXPOSE_TRANSACTIONS,
                    default=self.config_entry.options.get(CONF_EXPOSE_TRANSACTIONS, False),
                ): bool
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
