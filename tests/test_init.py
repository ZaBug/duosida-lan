"""End-to-end test in Home Assistant against the simulated wallbox.

Needs pytest-homeassistant-custom-component; skipped otherwise.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import SOURCE_USER  # noqa: E402
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE  # noqa: E402
from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import FlowResultType  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402

from test_client import DEVICE_ID, FakeWallbox  # noqa: E402

pytestmark = pytest.mark.enable_socket

DOMAIN = "duosida_lan"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def wallbox():
    fake = FakeWallbox()
    fake.port = await fake.start()
    yield fake
    await fake.close()


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


def _entity_id(hass: HomeAssistant, platform: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DEVICE_ID}_{key}")
    assert entity_id is not None, f"{platform} {key} not registered"
    return entity_id


async def test_flow_setup_and_control(hass: HomeAssistant, wallbox: FakeWallbox) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: wallbox.port}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Duosida Mode3@32A"
    entry = result["result"]
    assert entry.unique_id == DEVICE_ID
    await hass.async_block_till_done()

    # The probe session closed; the integration opens its own persistent one.
    status_id = _entity_id(hass, "sensor", "status")
    number_id = _entity_id(hass, "number", "max_current")
    switch_id = _entity_id(hass, "switch", "charging")
    toggle_id = _entity_id(hass, "button", "start_stop_charging")

    await _wait_for(lambda: hass.states.get(status_id).state == "available")
    await _wait_for(lambda: hass.states.get(number_id).state == "6.0")
    assert hass.states.get(number_id).attributes["max"] == 32
    assert hass.states.get(switch_id).state == "off"

    await hass.services.async_call("number", "set_value", {"entity_id": number_id, "value": 10}, blocking=True)
    assert wallbox.max_current == 10.0
    await _wait_for(lambda: hass.states.get(number_id).state == "10.0")

    await hass.services.async_call("button", "press", {"entity_id": toggle_id}, blocking=True)
    await _wait_for(lambda: hass.states.get(switch_id).state == "on")
    await _wait_for(lambda: wallbox.start_confs)

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch_id}, blocking=True)
    await _wait_for(lambda: hass.states.get(status_id).state == "finishing")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(status_id).state == STATE_UNAVAILABLE


async def test_flow_cannot_connect(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
