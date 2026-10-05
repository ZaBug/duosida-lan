"""UDP discovery of Duosida wallboxes on the local network.

The client sends ``smart_chargepile_search\\0`` to UDP 48899 (broadcast, or
unicast to one address); every wallbox answers to the sender's address and
port with ``ip,mac,type,firmware``, for example
``192.168.50.72,8cce4ee6b8ce,smart_wifi,V1.1@e37b36f+77``.

Discovery uses no TCP connection, so it never takes one of the few local
session slots of the wallbox. Verified live: broadcast and unicast both
answer from an ephemeral source port.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import socket

DISCOVERY_PORT = 48899
DISCOVERY_PAYLOAD = b"smart_chargepile_search\x00"
BROADCAST_ADDRESS = "255.255.255.255"


@dataclass(frozen=True, slots=True)
class DiscoveredWallbox:
    host: str
    mac: str  # lowercase, colon separated
    kind: str
    firmware: str


def format_mac(raw: str) -> str:
    digits = "".join(ch for ch in raw.lower() if ch in "0123456789abcdef")
    if len(digits) != 12:
        return raw.lower()
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def parse_reply(data: bytes, source_ip: str) -> DiscoveredWallbox | None:
    """Parse ``ip,mac,type,firmware``; returns None for anything else."""
    if data.startswith(DISCOVERY_PAYLOAD.rstrip(b"\x00")):
        return None  # our own broadcast
    parts = data.decode("utf-8", "replace").strip("\x00\r\n ").split(",")
    if len(parts) < 4:
        return None
    host = parts[0].strip() or source_ip
    mac = format_mac(parts[1].strip())
    if len(mac) != 17:
        return None
    return DiscoveredWallbox(host=host, mac=mac, kind=parts[2].strip(), firmware=",".join(parts[3:]).strip())


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.found: dict[str, DiscoveredWallbox] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        wallbox = parse_reply(data, addr[0])
        if wallbox is not None:
            self.found[wallbox.mac] = wallbox


async def async_discover(
    target: str = BROADCAST_ADDRESS,
    *,
    attempts: int = 3,
    interval: float = 1.0,
    port: int = DISCOVERY_PORT,
) -> list[DiscoveredWallbox]:
    """Search the network (or one address) and return the wallboxes that answered.

    The search is repeated ``attempts`` times because the 802.11b module may
    miss a single packet.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    transport, protocol = await loop.create_datagram_endpoint(_DiscoveryProtocol, sock=sock)
    try:
        for _ in range(attempts):
            transport.sendto(DISCOVERY_PAYLOAD, (target, port))
            await asyncio.sleep(interval)
            if target != BROADCAST_ADDRESS and protocol.found:
                break
    finally:
        transport.close()
    return sorted(protocol.found.values(), key=lambda w: tuple(int(x) if x.isdigit() else 0 for x in w.host.split(".")))


async def async_lookup(host: str, *, attempts: int = 3) -> DiscoveredWallbox | None:
    """Ask one address directly; returns its discovery answer, if any."""
    found = await async_discover(host, attempts=attempts)
    return found[0] if found else None
