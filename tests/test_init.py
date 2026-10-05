"""End-to-end test in Home Assistant against the simulated wallbox.

Needs pytest-homeassistant-custom-component; skipped otherwise.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER  # noqa: E402
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


FLOW = "custom_components.duosida_lan.config_flow"
MAC = "8c:ce:4e:e6:b8:ce"


def _found(host: str, mac: str = MAC):
    from custom_components.duosida_lan.discovery import DiscoveredWallbox

    return DiscoveredWallbox(host, mac, "smart_wifi", "V1.1@test")


@pytest.fixture
def discovered():
    """Control what UDP discovery returns (nothing by default)."""
    result: list = []
    with patch(f"{FLOW}.async_discover", AsyncMock(side_effect=lambda *a, **k: list(result))), patch(
        f"{FLOW}.async_lookup", AsyncMock(side_effect=lambda host, **k: next((w for w in result if w.host == host), None))
    ):
        yield result


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


async def test_integration_is_discoverable(hass: HomeAssistant) -> None:
    import custom_components
    from homeassistant import loader

    integrations = await loader.async_get_custom_components(hass)
    assert DOMAIN in integrations, (list(custom_components.__path__), sorted(integrations))


async def test_flow_setup_and_control(hass: HomeAssistant, wallbox: FakeWallbox, discovered) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

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


async def test_flow_cannot_connect(hass: HomeAssistant, discovered) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: 1}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def _add_via_pick(hass: HomeAssistant, wallbox: FakeWallbox, discovered):
    discovered.append(_found("127.0.0.1"))
    with patch(f"{FLOW}.DEFAULT_PORT", wallbox.port):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "pick"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.1"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def test_pick_discovered_wallbox(hass: HomeAssistant, wallbox: FakeWallbox, discovered) -> None:
    entry = await _add_via_pick(hass, wallbox, discovered)
    assert entry.data == {CONF_HOST: "127.0.0.1", CONF_PORT: wallbox.port, "mac": MAC}
    assert entry.unique_id == DEVICE_ID

    # A configured wallbox is not offered again.
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "manual"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_pick_manual_choice(hass: HomeAssistant, discovered) -> None:
    discovered.append(_found("127.0.0.1"))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "manual"})
    assert result["step_id"] == "manual"


async def test_reconfigure_changes_host(hass: HomeAssistant, wallbox: FakeWallbox, discovered) -> None:
    entry = await _add_via_pick(hass, wallbox, discovered)

    # Same MAC answering on a new address: accepted without opening a TCP session.
    discovered[:] = [_found("127.0.0.2")]
    connections_before = wallbox.connections
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure_pick"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.2"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()  # the reload runs in the background
    assert entry.data[CONF_HOST] == "127.0.0.2"
    assert wallbox.connections == connections_before

    # A different wallbox (other MAC) is refused.
    discovered[:] = [_found("127.0.0.3", "00:11:22:33:44:55")]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: "127.0.0.3"})
    assert result["errors"] == {"base": "wrong_device"}

    # Nothing answers over UDP: manual entry reports cannot_connect.
    discovered.clear()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["step_id"] == "reconfigure_manual"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.9", CONF_PORT: 9988}
    )
    assert result["errors"] == {"base": "cannot_connect"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
