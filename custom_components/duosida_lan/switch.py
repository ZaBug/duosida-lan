"""Charging switch for Duosida LAN: on = remote start, off = remote stop."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuosidaConfigEntry
from .client import DuosidaError
from .entity import DuosidaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: DuosidaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([DuosidaChargingSwitch(entry), DuosidaPlugAndChargeSwitch(entry)])


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


class DuosidaPlugAndChargeSwitch(DuosidaEntity, SwitchEntity):
    """VendorDirectWorkMode: start charging as soon as a car is plugged in.

    The state is read back from the wallbox configuration.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: DuosidaConfigEntry) -> None:
        super().__init__(entry, "plug_and_charge")

    @property
    def is_on(self) -> bool | None:
        return self.state_data.direct_work_mode

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, enabled: bool) -> None:
        try:
            await self._client.set_direct_work_mode(enabled)
        except DuosidaError as err:
            raise HomeAssistantError(f"Could not change plug and charge: {err}") from err
