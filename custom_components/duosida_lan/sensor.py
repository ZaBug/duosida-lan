"""Sensors for Duosida LAN."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuosidaConfigEntry
from .client import DuosidaState
from .entity import DuosidaEntity
from .protocol import CHARGE_POINT_ERROR, CHARGE_POINT_STATUS


@dataclass(frozen=True, kw_only=True)
class DuosidaSensorDescription(SensorEntityDescription):
    value_fn: Callable[[DuosidaState], float | int | str | None]


SENSORS: tuple[DuosidaSensorDescription, ...] = (
    DuosidaSensorDescription(
        key="status",
        device_class=SensorDeviceClass.ENUM,
        options=list(CHARGE_POINT_STATUS.values()),
        value_fn=lambda s: s.status if s.status in CHARGE_POINT_STATUS.values() else None,
    ),
    DuosidaSensorDescription(
        key="error",
        device_class=SensorDeviceClass.ENUM,
        options=list(CHARGE_POINT_ERROR.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.error_code if s.error_code in CHARGE_POINT_ERROR.values() else None,
    ),
    DuosidaSensorDescription(
        key="current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.current,
    ),
    DuosidaSensorDescription(
        key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: s.voltage,
    ),
    DuosidaSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda s: s.power,
    ),
    DuosidaSensorDescription(
        key="session_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda s: s.session_energy,
    ),
    DuosidaSensorDescription(
        # The wallbox register restarts from 0 after a power cycle;
        # total_increasing handles that as a meter reset.
        key="energy_register",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda s: s.energy_register,
    ),
    DuosidaSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.temperature,
    ),
    DuosidaSensorDescription(
        key="max_current_setting",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda s: s.max_current_setting,
    ),
    DuosidaSensorDescription(
        key="transaction_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.transaction_id,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: DuosidaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(DuosidaSensor(entry, description) for description in SENSORS)


class DuosidaSensor(DuosidaEntity, SensorEntity):
    entity_description: DuosidaSensorDescription

    def __init__(self, entry: DuosidaConfigEntry, description: DuosidaSensorDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | None:
        return self.entity_description.value_fn(self.state_data)
