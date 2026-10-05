"""Maximum charging current for Duosida LAN."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuosidaConfigEntry
from .client import DuosidaError
from .const import DEFAULT_MAX_CURRENT, MIN_CURRENT
from .entity import DuosidaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: DuosidaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([DuosidaMaxCurrentNumber(entry)])


class DuosidaMaxCurrentNumber(DuosidaEntity, NumberEntity):
    """VendorMaxWorkCurrent. The state is the value read back from the wallbox."""

    _attr_device_class = NumberDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_native_min_value = MIN_CURRENT
    _attr_native_max_value = DEFAULT_MAX_CURRENT
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: DuosidaConfigEntry) -> None:
        super().__init__(entry, "max_current")

    @property
    def native_max_value(self) -> float:
        # "DUOSIDA Mode3@32A" -> 32
        identity = self.state_data.identity
        if identity and "@" in identity.model:
            rating = identity.model.rsplit("@", 1)[1].rstrip("Aa")
            if rating.isdigit() and MIN_CURRENT <= int(rating) <= DEFAULT_MAX_CURRENT:
                return float(rating)
        return float(DEFAULT_MAX_CURRENT)

    @property
    def native_value(self) -> float | None:
        return self.state_data.max_current_setting

    async def async_set_native_value(self, value: float) -> None:
        try:
            await self._client.set_max_current(int(round(value)))
        except DuosidaError as err:
            raise HomeAssistantError(f"Could not set the maximum current: {err}") from err
