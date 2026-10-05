"""Config flow for Duosida LAN."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .client import DuosidaError, async_probe
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .protocol import DEFAULT_PORT, ProtocolError

_LOGGER = logging.getLogger(__name__)


class DuosidaLanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a wallbox by IP address."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            # Check before connecting: a second session next to a running one
            # can lock the wallbox out.
            self._async_abort_entries_match({CONF_HOST: host})
            try:
                identity = await async_probe(host, port)
            except (OSError, TimeoutError, DuosidaError, ProtocolError) as err:
                _LOGGER.debug("Duosida probe of %s:%s failed: %s", host, port, err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(identity.device_id or host)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                # "DUOSIDA Mode3@32A" -> "Duosida Mode3@32A"
                model = identity.model
                if model.upper().startswith("DUOSIDA"):
                    model = model[len("DUOSIDA"):].strip()
                title = f"Duosida {model}".strip()
                return self.async_create_entry(title=title, data={CONF_HOST: host, CONF_PORT: port})

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DuosidaLanOptionsFlow()


class DuosidaLanOptionsFlow(OptionsFlow):
    """Poll interval. Changing it reloads the entry (reconnects after ~1 min)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=current): vol.All(
                    int, vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
