"""Buttons for Duosida LAN: start, stop, start/stop toggle, refresh."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DuosidaConfigEntry
from .client import DuosidaClient, DuosidaError
from .entity import DuosidaEntity


async def _toggle(client: DuosidaClient) -> None:
    """Stop when a transaction runs, otherwise start.

    Same contract as the cloud integration's start/stop button, so
    ev_solar_manager's charger_start_stop_button can point at it.
    """
    if client.state.session_active:
        await client.stop_charging()
    else:
        await client.start_charging()


@dataclass(frozen=True, kw_only=True)
class DuosidaButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[DuosidaClient], Awaitable[None]]


BUTTONS: tuple[DuosidaButtonDescription, ...] = (
    DuosidaButtonDescription(key="start_charging", press_fn=lambda c: c.start_charging()),
    DuosidaButtonDescription(key="stop_charging", press_fn=lambda c: c.stop_charging()),
    DuosidaButtonDescription(key="start_stop_charging", press_fn=_toggle),
    DuosidaButtonDescription(
        key="refresh", entity_category=EntityCategory.DIAGNOSTIC, press_fn=lambda c: c.refresh()
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: DuosidaConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(DuosidaButton(entry, description) for description in BUTTONS)


class DuosidaButton(DuosidaEntity, ButtonEntity):
    entity_description: DuosidaButtonDescription

    def __init__(self, entry: DuosidaConfigEntry, description: DuosidaButtonDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        try:
            await self.entity_description.press_fn(self._client)
        except DuosidaError as err:
            raise HomeAssistantError(str(err)) from err
