"""Duosida LAN: local control of Duosida EV wallboxes over TCP 9988."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import DuosidaClient, DuosidaState
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN, MANUFACTURER
from .protocol import DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]


@dataclass(slots=True)
class DuosidaRuntimeData:
    client: DuosidaClient
    coordinator: DataUpdateCoordinator[DuosidaState]


type DuosidaConfigEntry = ConfigEntry[DuosidaRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: DuosidaConfigEntry) -> bool:
    poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    coordinator: DataUpdateCoordinator[DuosidaState] = DataUpdateCoordinator(
        hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.data[CONF_HOST]}"
    )

    registered_firmware: list[str | None] = [None]

    @callback
    def _on_update(state: DuosidaState) -> None:
        identity = state.identity
        if identity is not None and identity.firmware != registered_firmware[0]:
            # Fill model / serial / firmware once the wallbox announced them.
            registered_firmware[0] = identity.firmware
            dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
                manufacturer=MANUFACTURER,
                model=identity.model,
                serial_number=identity.device_id,
                sw_version=identity.firmware,
                name=entry.title,
            )
        coordinator.async_set_updated_data(state)

    client = DuosidaClient(
        entry.data[CONF_HOST],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
        poll_interval=poll_interval,
        on_update=_on_update,
    )
    coordinator.data = client.state
    entry.runtime_data = DuosidaRuntimeData(client, coordinator)

    # The session is opened in the background: the wallbox may refuse new
    # connections for ~30 s after a previous one closed (e.g. a quick restart),
    # and entities simply stay unavailable until it answers.
    client.start()

    async def _on_hass_stop(_event: Event) -> None:
        await client.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_hass_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DuosidaConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.stop()
    return unloaded

