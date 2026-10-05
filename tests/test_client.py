"""Client tests against a simulated wallbox on localhost."""

from __future__ import annotations

import asyncio
import struct

import pytest

from conftest import client as c
from conftest import protocol as p

pytestmark = pytest.mark.enable_socket

DEVICE_ID = "0000000000000000001"


class FakeWallbox:
    """Answers like the real wallbox did in the live tests."""

    def __init__(self) -> None:
        self.status = 0  # available
        self.max_current = 6.0
        self.direct_work_mode = True
        self.transaction_id: int | None = None
        self.received: list[p.Frame] = []
        self.start_confs: list[int] = []
        self.connections = 0
        self.server: asyncio.base_events.Server | None = None
        self._own_id = 134256000
        self._writer: asyncio.StreamWriter | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    def _next(self) -> int:
        self._own_id += 1
        return self._own_id

    def _send(self, kind: int, payload: bytes, message_id: int | None = None) -> None:
        assert self._writer is not None
        mid = self._next() if message_id is None else message_id
        self._writer.write(
            p.field_bytes(kind, payload)
            + p.field_bytes(p.FIELD_CLIENT_ID, DEVICE_ID.encode())
            + p.field_varint(p.FIELD_MESSAGE_ID, mid)
        )

    def _status(self) -> None:
        self._send(p.STATUS_NOTIFICATION_REQ, p.field_varint(1, 1) + p.field_varint(2, 6) + p.field_varint(4, self.status))

    def _meter(self, with_transaction: bool = False) -> None:
        amps = "6.00" if self.status == 2 else "0.00"
        value = p.field_varint(1, 0)
        for text, measurand, unit in ((amps, 14, 10), ("0.05", 5, 1), ("230.0", 17, 11), ("1380.0", 9, 4)):
            sample = p.field_bytes(1, text.encode()) + p.field_varint(4, measurand) + p.field_varint(7, unit)
            value += p.field_bytes(2, sample)
        payload = p.field_varint(1, 1)
        if with_transaction and self.transaction_id:
            payload += p.field_varint(2, self.transaction_id)
        self._send(p.METER_VALUES_REQ, payload + p.field_bytes(3, value))

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writer = writer
        splitter = p.FrameSplitter()
        try:
            while chunk := await reader.read(4096):
                for raw in splitter.feed(chunk):
                    self._on_frame(p.parse_frame(raw))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _on_frame(self, frame: p.Frame) -> None:
        self.received.append(frame)
        kind, mid = frame.kind, frame.message_id
        if kind == p.TRIGGER_MESSAGE_REQ:
            requested = p.first(p.decode(frame.payload), 1, 0) or 0
            self._send(p.TRIGGER_MESSAGE_CONF, p.field_varint(1, 0), mid)
            if requested == p.TRIGGER_BOOT:
                identity = (
                    p.field_bytes(2, b"DUOSIDA Mode3@32A") + p.field_bytes(3, DEVICE_ID.encode())
                    + p.field_bytes(4, b"UCHEN") + p.field_bytes(5, b"V2.5@test")
                )
                self._send(p.BOOT_NOTIFICATION_REQ, identity)
            elif requested == p.TRIGGER_STATUS:
                self._status()
            elif requested == p.TRIGGER_METER_VALUES:
                self._meter()
        elif kind == p.DATA_TRANSFER_REQ:
            pulled = bytes([3 << 3 | 5]) + struct.pack("<f", self.max_current) + p.field_varint(10, int(self.direct_work_mode))
            self._send(p.DATA_TRANSFER_CONF, p.field_bytes(15, pulled), mid)
        elif kind == p.CHANGE_CONFIGURATION_REQ:
            fields = p.decode(frame.payload)
            key, value = p.as_text(p.first(fields, 1, 2)), p.as_text(p.first(fields, 2, 2))
            if key == p.MAX_CURRENT_KEY:
                self.max_current = float(value)
            elif key == p.DIRECT_WORK_MODE_KEY:
                self.direct_work_mode = value == "1"
            else:
                self._send(p.CHANGE_CONFIGURATION_CONF, p.field_varint(1, 1), mid)  # Rejected
                return
            self._send(p.CHANGE_CONFIGURATION_CONF, b"", mid)
        elif kind == p.REMOTE_START_REQ:
            self._send(p.REMOTE_START_CONF, b"", mid)
            self.status = 2
            self._status()
            tag = p.field_bytes(1, p.REMOTE_ID_TAG.encode())
            self._send(p.START_TRANSACTION_REQ, p.field_varint(1, 1) + p.field_bytes(2, tag) + p.field_varint(3, 101))
        elif kind == p.START_TRANSACTION_CONF:
            self.transaction_id = p.first(p.decode(frame.payload), 2, 0)
            self.start_confs.append(self.transaction_id)
            self._meter(with_transaction=True)
        elif kind == p.REMOTE_STOP_REQ:
            requested_tx = p.first(p.decode(frame.payload), 1, 0)
            if requested_tx != self.transaction_id:
                self._send(p.REMOTE_STOP_CONF, p.field_varint(1, 1), mid)
                return
            self._send(p.REMOTE_STOP_CONF, b"", mid)
            self.status = 5
            self._status()
            self._send(p.STOP_TRANSACTION_REQ, p.field_varint(2, 101) + p.field_varint(4, requested_tx) + p.field_varint(5, 4))


@pytest.fixture
async def wallbox():
    fake = FakeWallbox()
    port = await fake.start()
    fake.port = port
    yield fake
    await fake.close()


async def _connected_client(wallbox: FakeWallbox, **kwargs) -> c.DuosidaClient:
    client = c.DuosidaClient("127.0.0.1", wallbox.port, poll_interval=0.2, **kwargs)
    client.start()
    assert await client.async_wait_ready(5)
    return client


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


async def test_probe_reads_identity(wallbox: FakeWallbox) -> None:
    identity = await c.async_probe("127.0.0.1", wallbox.port)
    assert identity.model == "DUOSIDA Mode3@32A"
    assert identity.device_id == DEVICE_ID


async def test_connect_and_poll(wallbox: FakeWallbox) -> None:
    updates: list[c.DuosidaState] = []
    client = await _connected_client(wallbox, on_update=updates.append)
    try:
        await _wait_for(lambda: client.state.status == "available" and client.state.max_current_setting == 6.0)
        assert client.state.connected
        assert client.state.voltage == pytest.approx(230.0)
        assert client.state.identity.firmware == "V2.5@test"
        assert updates and updates[-1] is client.state
    finally:
        await client.stop()
    assert not client.state.connected
    assert wallbox.connections == 1


async def test_set_max_current_reads_back(wallbox: FakeWallbox) -> None:
    client = await _connected_client(wallbox)
    try:
        await client.set_max_current(10)
        assert wallbox.max_current == 10.0
        await _wait_for(lambda: client.state.max_current_setting == 10.0)
    finally:
        await client.stop()


async def test_start_confirms_transaction_and_stop_uses_it(wallbox: FakeWallbox) -> None:
    client = await _connected_client(wallbox)
    try:
        await client.start_charging()
        await _wait_for(lambda: wallbox.start_confs and client.state.transaction_id is not None)
        assert client.state.transaction_id == wallbox.start_confs[0]
        await _wait_for(lambda: client.state.status == "charging")
        assert client.state.session_active

        await client.stop_charging()
        await _wait_for(lambda: client.state.status == "finishing")
        assert client.state.transaction_id is None
    finally:
        await client.stop()


async def test_stop_without_transaction_fails(wallbox: FakeWallbox) -> None:
    client = await _connected_client(wallbox)
    try:
        with pytest.raises(c.DuosidaCommandError):
            await client.stop_charging()
    finally:
        await client.stop()


async def test_never_sends_boot_notification_conf(wallbox: FakeWallbox) -> None:
    client = await _connected_client(wallbox)
    try:
        await client.set_max_current(8)
        await client.start_charging()
        await asyncio.sleep(0.5)
    finally:
        await client.stop()
    kinds = {frame.kind for frame in wallbox.received}
    assert 3 not in kinds  # BootNotificationConf
    assert p.CHANGE_CONFIGURATION_REQ in kinds


async def test_commands_fail_when_not_connected() -> None:
    client = c.DuosidaClient("127.0.0.1", 1)
    with pytest.raises(c.DuosidaConnectionError):
        await client.set_max_current(10)


async def test_reconnects_after_disconnect(wallbox: FakeWallbox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(c, "RECONNECT_DELAYS_S", (0.1,))
    client = await _connected_client(wallbox)
    try:
        wallbox._writer.close()
        await _wait_for(lambda: not client.connected)
        await _wait_for(lambda: client.connected, timeout=5)
        assert wallbox.connections == 2
    finally:
        await client.stop()


async def test_plug_and_charge_reads_back(wallbox: FakeWallbox) -> None:
    client = await _connected_client(wallbox)
    try:
        await _wait_for(lambda: client.state.direct_work_mode is True)
        await client.set_direct_work_mode(False)
        assert wallbox.direct_work_mode is False
        await _wait_for(lambda: client.state.direct_work_mode is False)
        await client.set_direct_work_mode(True)
        await _wait_for(lambda: client.state.direct_work_mode is True)
    finally:
        await client.stop()
