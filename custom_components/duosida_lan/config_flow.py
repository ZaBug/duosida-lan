"""Config flow for Duosida LAN."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PORT
from homeassistant.core import callback

from .client import DuosidaError, async_probe
from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .discovery import DiscoveredWallbox, async_discover, async_lookup
from .protocol import DEFAULT_PORT, ProtocolError

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"


def _label(wallbox: DiscoveredWallbox) -> str:
    return f"{wallbox.host} ({wallbox.mac})"


def _port_field(default: int) -> dict[Any, Any]:
    return {vol.Required(CONF_PORT, default=default): vol.All(int, vol.Range(min=1, max=65535))}


class DuosidaLanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a wallbox: pick one found on the network, or enter its IP address."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredWallbox] = {}

    async def _async_discover(self) -> dict[str, DiscoveredWallbox]:
        try:
            found = await async_discover()
        except OSError as err:
            _LOGGER.debug("Duosida discovery failed: %s", err)
            return {}
        return {wallbox.host: wallbox for wallbox in found}

    # --- add a wallbox ------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        configured = {entry.data.get(CONF_HOST) for entry in self._async_current_entries()}
        configured_macs = {entry.data.get(CONF_MAC) for entry in self._async_current_entries()}
        self._discovered = {
            host: wallbox
            for host, wallbox in (await self._async_discover()).items()
            if host not in configured and wallbox.mac not in configured_macs
        }
        if not self._discovered:
            return await self.async_step_manual()
        return await self.async_step_pick()

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = user_input[CONF_HOST]
            if choice == MANUAL:
                return await self.async_step_manual()
            wallbox = self._discovered[choice]
            result = await self._async_create(wallbox.host, DEFAULT_PORT, wallbox.mac, errors)
            if result is not None:
                return result

        options = {host: _label(wallbox) for host, wallbox in self._discovered.items()}
        options[MANUAL] = "Enter the IP address manually"
        schema = vol.Schema({vol.Required(CONF_HOST, default=next(iter(options))): vol.In(options)})
        return self.async_show_form(step_id="pick", data_schema=schema, errors=errors)

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            wallbox = await self._async_safe_lookup(host)
            result = await self._async_create(host, user_input[CONF_PORT], wallbox.mac if wallbox else None, errors)
            if result is not None:
                return result

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
                **_port_field(DEFAULT_PORT),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    async def _async_create(
        self, host: str, port: int, mac: str | None, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        # Check before connecting: a second TCP session next to a running one
        # can lock the wallbox out.
        self._async_abort_entries_match({CONF_HOST: host})
        try:
            identity = await async_probe(host, port)
        except (OSError, TimeoutError, DuosidaError, ProtocolError) as err:
            _LOGGER.debug("Duosida probe of %s:%s failed: %s", host, port, err)
            errors["base"] = "cannot_connect"
            return None
        data = {CONF_HOST: host, CONF_PORT: port}
        if mac:
            data[CONF_MAC] = mac
        await self.async_set_unique_id(identity.device_id or host)
        self._abort_if_unique_id_configured(updates=data)
        # "DUOSIDA Mode3@32A" -> "Duosida Mode3@32A"
        model = identity.model
        if model.upper().startswith("DUOSIDA"):
            model = model[len("DUOSIDA"):].strip()
        return self.async_create_entry(title=f"Duosida {model}".strip(), data=data)

    async def _async_safe_lookup(self, host: str) -> DiscoveredWallbox | None:
        try:
            return await async_lookup(host)
        except OSError as err:
            _LOGGER.debug("Duosida lookup of %s failed: %s", host, err)
            return None

    # --- change the address of a configured wallbox ---------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._discovered = await self._async_discover()
        if not self._discovered:
            return await self.async_step_reconfigure_manual()
        return await self.async_step_reconfigure_pick()

    async def async_step_reconfigure_pick(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = user_input[CONF_HOST]
            if choice == MANUAL:
                return await self.async_step_reconfigure_manual()
            result = self._async_apply(entry, self._discovered[choice], entry.data.get(CONF_PORT, DEFAULT_PORT), errors)
            if result is not None:
                return result

        options = {host: _label(wallbox) for host, wallbox in self._discovered.items()}
        options[MANUAL] = "Enter the IP address manually"
        current = entry.data[CONF_HOST]
        default = current if current in options else next(iter(options))
        schema = vol.Schema({vol.Required(CONF_HOST, default=default): vol.In(options)})
        return self.async_show_form(step_id="reconfigure_pick", data_schema=schema, errors=errors)

    async def async_step_reconfigure_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            # Validate over UDP only: the integration already holds the TCP
            # session, and a second one can lock the wallbox out.
            wallbox = await self._async_safe_lookup(host)
            if wallbox is None:
                errors["base"] = "cannot_connect"
            else:
                result = self._async_apply(entry, wallbox, user_input[CONF_PORT], errors)
                if result is not None:
                    return result

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, entry.data[CONF_HOST])): str,
                **_port_field(entry.data.get(CONF_PORT, DEFAULT_PORT)),
            }
        )
        return self.async_show_form(step_id="reconfigure_manual", data_schema=schema, errors=errors)

    def _async_apply(
        self, entry: ConfigEntry, wallbox: DiscoveredWallbox, port: int, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        known_mac = entry.data.get(CONF_MAC)
        if known_mac and known_mac != wallbox.mac:
            errors["base"] = "wrong_device"
            return None
        return self.async_update_reload_and_abort(
            entry, data_updates={CONF_HOST: wallbox.host, CONF_PORT: port, CONF_MAC: wallbox.mac}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DuosidaLanOptionsFlow()


class DuosidaLanOptionsFlow(OptionsFlowWithReload):
    """Poll interval. Saving reloads the entry (reconnects after ~1 min)."""

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
