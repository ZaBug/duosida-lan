"""Duosida LAN: local control of Duosida EV wallboxes over TCP 9988."""

from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .client import DuosidaClient, DuosidaState
from .const import CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL, DOMAIN
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

    @callback
    def _on_update(state: DuosidaState) -> None:
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
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DuosidaConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.stop()
    return unloaded


async def _async_reload_on_options(hass: HomeAssistant, entry: DuosidaConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
