"""Base entity for Duosida LAN."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from . import DuosidaConfigEntry
from .client import DuosidaClient, DuosidaState
from .const import DOMAIN, MANUFACTURER


class DuosidaEntity(CoordinatorEntity[DataUpdateCoordinator[DuosidaState]]):
    """Shared device info and availability."""

    _attr_has_entity_name = True

    def __init__(self, entry: DuosidaConfigEntry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self._client: DuosidaClient = entry.runtime_data.client
        unique_base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{unique_base}_{key}"
        self._attr_translation_key = key
        identity = self._client.state.identity
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, unique_base)},
            manufacturer=MANUFACTURER,
            model=identity.model if identity else None,
            serial_number=identity.device_id if identity else None,
            sw_version=identity.firmware if identity else None,
            name=entry.title,
        )

    @property
    def state_data(self) -> DuosidaState:
        return self.coordinator.data

    @property
    def available(self) -> bool:
        return self.coordinator.data.connected
