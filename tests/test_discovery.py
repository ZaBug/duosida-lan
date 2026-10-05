"""UDP discovery tests against a simulated responder on localhost."""

from __future__ import annotations

import asyncio

import pytest

from conftest import discovery as d

pytestmark = pytest.mark.enable_socket

REPLY = b"192.168.50.72,8cce4ee6b8ce,smart_wifi,V1.1@e37b36f+77"


def test_parse_reply() -> None:
    wallbox = d.parse_reply(REPLY, "192.168.50.72")
    assert wallbox == d.DiscoveredWallbox("192.168.50.72", "8c:ce:4e:e6:b8:ce", "smart_wifi", "V1.1@e37b36f+77")


@pytest.mark.parametrize("data", [d.DISCOVERY_PAYLOAD, b"hello", b"1.2.3.4,nomac,x,y", b""])
def test_parse_reply_ignores_other_packets(data: bytes) -> None:
    assert d.parse_reply(data, "1.2.3.4") is None


def test_format_mac() -> None:
    assert d.format_mac("8CCE4EE6B8CE") == "8c:ce:4e:e6:b8:ce"
    assert d.format_mac("8c-ce-4e-e6-b8-ce") == "8c:ce:4e:e6:b8:ce"


class _Responder(asyncio.DatagramProtocol):
    def __init__(self, drop_first: int = 0) -> None:
        self.requests = 0
        self.drop_first = drop_first

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        self.requests += 1
        if data == d.DISCOVERY_PAYLOAD and self.requests > self.drop_first:
            self.transport.sendto(REPLY, addr)


async def _responder(drop_first: int = 0):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: _Responder(drop_first), local_addr=("127.0.0.1", 0)
    )
    return transport, protocol, transport.get_extra_info("sockname")[1]


async def test_lookup_answers() -> None:
    transport, protocol, port = await _responder()
    try:
        found = await d.async_discover("127.0.0.1", attempts=3, interval=0.2, port=port)
    finally:
        transport.close()
    assert [w.mac for w in found] == ["8c:ce:4e:e6:b8:ce"]
    assert protocol.requests == 1  # unicast stops after the first answer


async def test_lookup_retries_lost_packets() -> None:
    transport, protocol, port = await _responder(drop_first=2)
    try:
        found = await d.async_discover("127.0.0.1", attempts=3, interval=0.2, port=port)
    finally:
        transport.close()
    assert len(found) == 1
    assert protocol.requests == 3


async def test_lookup_no_answer() -> None:
    transport, protocol, port = await _responder(drop_first=99)
    try:
        found = await d.async_discover("127.0.0.1", attempts=2, interval=0.1, port=port)
    finally:
        transport.close()
    assert found == []
