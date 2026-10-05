"""Charging switch for Duosida LAN: on = remote start, off = remote stop."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuosidaConfigEntry
from .client import DuosidaError
from .entity import DuosidaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: DuosidaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([DuosidaChargingSwitch(entry)])


class DuosidaChargingSwitch(DuosidaEntity, SwitchEntity):
    """On while a transaction runs (charging or paused by the car / wallbox)."""

    def __init__(self, entry: DuosidaConfigEntry) -> None:
        super().__init__(entry, "charging")

    @property
    def is_on(self) -> bool | None:
        if self.state_data.status is None:
            return None
        return self.state_data.session_active

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._client.start_charging()
        except DuosidaError as err:
            raise HomeAssistantError(f"Could not start charging: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._client.stop_charging()
        except DuosidaError as err:
            raise HomeAssistantError(f"Could not stop charging: {err}") from err
