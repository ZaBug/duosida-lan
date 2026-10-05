"""Asynchronous client that keeps one persistent session with the wallbox.

Lessons from live tests that shape this client:

* Keep exactly one TCP session. After a session closes, the wallbox keeps the
  slot busy for about 30 s. Reconnecting every few seconds made it refuse
  every connection for minutes; a second parallel session once locked it out
  until a power cycle.
* Never send BootNotificationConf. Answering the BootNotification as a
  central system preceded that lock-out.
* While a local session is open, the wallbox sends transaction messages
  (StartTransaction / StopTransaction) to it and retries until they are
  confirmed. This client confirms them and assigns the transaction id, so
  remote stop keeps working.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import logging
import time

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10.0
IDENTITY_TIMEOUT_S = 15.0
COMMAND_TIMEOUT_S = 15.0
CONFIG_POLL_INTERVAL_S = 60.0
RECONNECT_DELAYS_S = (60.0, 120.0, 300.0)


class DuosidaError(Exception):
    """Base error."""


class DuosidaConnectionError(DuosidaError):
    """The wallbox cannot be reached or the session is not ready."""


class DuosidaCommandError(DuosidaError):
    """The wallbox rejected a command or did not answer it."""


@dataclass(slots=True)
class DuosidaState:
    """Latest known wallbox state. Replaced (not mutated) on every update."""

    connected: bool = False
    identity: p.Identity | None = None
    status: str | None = None
    error_code: str | None = None
    status_info: str | None = None
    current: float | None = None
    voltage: float | None = None
    power: float | None = None
    temperature: float | None = None
    session_energy: float | None = None
    energy_register: float | None = None
    transaction_id: int | None = None
    max_current_setting: float | None = None
    direct_work_mode: bool | None = None
    last_seen: float | None = None
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def charging(self) -> bool:
        return self.status == "charging"

    @property
    def session_active(self) -> bool:
        """A transaction is running (charging or paused by the car / wallbox)."""
        return self.status in ("charging", "suspended_ev", "suspended_evse")


async def async_probe(host: str, port: int = p.DEFAULT_PORT) -> p.Identity:
    """Open a short session, read the identity and close. Used by the config flow."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT_S)
    try:
        writer.write(p.HELLO)
        await writer.drain()
        splitter = p.FrameSplitter()
        deadline = time.monotonic() + IDENTITY_TIMEOUT_S
        while (remaining := deadline - time.monotonic()) > 0:
            chunk = await asyncio.wait_for(reader.read(4096), remaining)
            if not chunk:
                raise DuosidaConnectionError("wallbox closed the connection")
            for raw in splitter.feed(chunk):
                frame = p.parse_frame(raw)
                if frame.kind == p.BOOT_NOTIFICATION_REQ:
                    return p.parse_identity(frame.payload)
        raise DuosidaConnectionError("wallbox did not send its identity")
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 5)
        except (OSError, TimeoutError):
            pass


class DuosidaClient:
    """Persistent local session with one Duosida wallbox."""

    def __init__(
        self,
        host: str,
        port: int = p.DEFAULT_PORT,
        *,
        poll_interval: float = 15.0,
        on_update: Callable[[DuosidaState], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self._on_update = on_update
        self._state = DuosidaState()
        self._runner: asyncio.Task[None] | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._device_id: str | None = None
        self._ready = asyncio.Event()
        self._pending: dict[int, asyncio.Future[p.Frame]] = {}
        self._send_lock = asyncio.Lock()
        self._message_id = int(time.time()) & 0x0FFFFFFF
        self._last_frame_at = 0.0
        self._reconnect_attempt = 0

    # --- public API -----------------------------------------------------------

    @property
    def state(self) -> DuosidaState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    def start(self) -> None:
        if self._runner is None or self._runner.done():
            self._runner = asyncio.get_running_loop().create_task(
                self._run(), name=f"duosida_lan {self.host}"
            )

    async def stop(self) -> None:
        runner, self._runner = self._runner, None
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        await self._close()

    async def async_wait_ready(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def set_max_current(self, amps: int) -> None:
        frame = await self._request(lambda mid: p.build_set_max_current(self._id(), mid, amps))
        status = p.parse_conf_status(frame.payload)
        if status not in ("Accepted", "RebootRequired"):
            raise DuosidaCommandError(f"wallbox answered {status} to max current {amps} A")
        self._update(max_current_setting=float(amps))
        # Read the stored value back; the answer updates the state when it arrives.
        await self._send(lambda mid: p.build_pull_configuration(self._id(), mid))

    async def start_charging(self) -> None:
        frame = await self._request(lambda mid: p.build_remote_start(self._id(), mid))
        status = p.parse_conf_status(frame.payload)
        if status != "Accepted":
            raise DuosidaCommandError(f"wallbox answered {status} to remote start")

    async def stop_charging(self) -> None:
        transaction_id = self._state.transaction_id
        if transaction_id is None:
            raise DuosidaCommandError("no transaction id known yet, cannot stop remotely")
        frame = await self._request(lambda mid: p.build_remote_stop(self._id(), mid, transaction_id))
        status = p.parse_conf_status(frame.payload)
        if status != "Accepted":
            raise DuosidaCommandError(f"wallbox answered {status} to remote stop")

    async def refresh(self) -> None:
        await self._poll_once(include_config=True)

    # --- session lifecycle ----------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, DuosidaError, p.ProtocolError) as err:
                _LOGGER.warning("Duosida %s: session ended: %s", self.host, err)
            except Exception:  # noqa: BLE001 - keep the runner alive
                _LOGGER.exception("Duosida %s: unexpected error in session", self.host)
            await self._close()
            delay = RECONNECT_DELAYS_S[min(self._reconnect_attempt, len(RECONNECT_DELAYS_S) - 1)]
            self._reconnect_attempt += 1
            _LOGGER.debug("Duosida %s: reconnecting in %.0f s", self.host, delay)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT_S
        )
        self._writer = writer
        self._last_frame_at = time.monotonic()
        writer.write(p.HELLO)
        await writer.drain()
        splitter = p.FrameSplitter()
        poller: asyncio.Task[None] | None = None
        try:
            while True:
                timeout = IDENTITY_TIMEOUT_S if not self._ready.is_set() else max(3 * self.poll_interval, 45.0)
                chunk = await asyncio.wait_for(reader.read(4096), timeout)
                if not chunk:
                    raise DuosidaConnectionError("wallbox closed the connection")
                self._last_frame_at = time.monotonic()
                for raw in splitter.feed(chunk):
                    await self._handle(p.parse_frame(raw))
                if self._ready.is_set() and poller is None:
                    poller = asyncio.get_running_loop().create_task(self._poll_loop())
        finally:
            if poller is not None:
                poller.cancel()
                try:
                    await poller
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - already logged by the poller
                    pass

    async def _close(self) -> None:
        was_ready = self._ready.is_set()
        self._ready.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DuosidaConnectionError("session closed"))
        self._pending.clear()
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 5)
            except (OSError, TimeoutError):
                pass
        if was_ready or self._state.connected:
            self._update(connected=False)

    async def _poll_loop(self) -> None:
        last_config = 0.0
        while True:
            include_config = time.monotonic() - last_config >= CONFIG_POLL_INTERVAL_S
            try:
                await self._poll_once(include_config=include_config)
            except (OSError, DuosidaError) as err:
                # The reader notices the broken session and reconnects.
                _LOGGER.debug("Duosida %s: poll failed: %s", self.host, err)
                return
            if include_config:
                last_config = time.monotonic()
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self, include_config: bool) -> None:
        await self._send(lambda mid: p.build_trigger(self._id(), mid, p.TRIGGER_STATUS))
        await self._send(lambda mid: p.build_trigger(self._id(), mid, p.TRIGGER_METER_VALUES))
        if include_config:
            await self._send(lambda mid: p.build_pull_configuration(self._id(), mid))

    # --- incoming messages ----------------------------------------------------

    async def _handle(self, frame: p.Frame) -> None:
        if frame.message_id is not None and frame.kind is not None:
            future = self._pending.pop(frame.message_id, None)
            if future is not None and not future.done() and frame.kind % 2 == 1:
                # *.conf messages have odd field numbers and echo our message id.
                future.set_result(frame)

        kind = frame.kind
        if kind == p.BOOT_NOTIFICATION_REQ:
            identity = p.parse_identity(frame.payload)
            if not self._ready.is_set():
                self._device_id = identity.device_id or frame.client_id
                self._reconnect_attempt = 0
                self._ready.set()
                _LOGGER.info("Duosida %s: connected to %s (%s)", self.host, identity.model, identity.firmware)
                self._update(connected=True, identity=identity)
            # Deliberately no BootNotificationConf (see module docstring).
        elif kind == p.STATUS_NOTIFICATION_REQ:
            await self._reply(p.build_ack(p.STATUS_NOTIFICATION_CONF, self._id(), frame.message_id))
            notification = p.parse_status_notification(frame.payload)
            changes: dict[str, object] = {
                "status": notification.status,
                "error_code": notification.error_code,
                "status_info": notification.info,
            }
            if notification.status in ("available", "finishing", "faulted", "unavailable"):
                changes.update(current=0.0, power=0.0)
            if notification.status in ("available", "finishing"):
                # No running transaction; also drops an id left over from a
                # stop that happened while we were disconnected.
                changes["transaction_id"] = None
            self._update(**changes)
        elif kind == p.METER_VALUES_REQ:
            await self._reply(p.build_ack(p.METER_VALUES_CONF, self._id(), frame.message_id))
            meter = p.parse_meter_values(frame.payload)
            changes = {
                name: getattr(meter, name)
                for name in ("current", "voltage", "power", "temperature", "session_energy", "energy_register")
                if getattr(meter, name) is not None
            }
            if meter.transaction_id:
                changes["transaction_id"] = meter.transaction_id
            self._update(**changes)
        elif kind == p.START_TRANSACTION_REQ:
            start = p.parse_start_transaction(frame.payload)
            transaction_id = self._state.transaction_id or self._new_transaction_id()
            await self._reply(p.build_start_transaction_conf(self._id(), frame.message_id, transaction_id))
            _LOGGER.info(
                "Duosida %s: transaction %s started (idTag %s, meter %s Wh)",
                self.host, transaction_id, start.id_tag, start.meter_start_wh,
            )
            self._update(transaction_id=transaction_id)
        elif kind == p.STOP_TRANSACTION_REQ:
            stop = p.parse_stop_transaction(frame.payload)
            await self._reply(p.build_ack(p.STOP_TRANSACTION_CONF, self._id(), frame.message_id))
            _LOGGER.info(
                "Duosida %s: transaction %s stopped (meter %s Wh, reason %s)",
                self.host, stop.transaction_id, stop.meter_stop_wh, stop.reason,
            )
            self._update(transaction_id=None)
        elif kind == p.HEARTBEAT_REQ:
            await self._reply(p.build_heartbeat_conf(self._id(), frame.message_id, int(time.time())))
        elif kind == p.DATA_TRANSFER_REQ and p.is_vendor_status(frame.payload):
            await self._reply(p.build_vendor_status_conf(self._id(), frame.message_id))
        elif kind == p.DATA_TRANSFER_CONF:
            config = p.parse_data_transfer_conf(frame.payload)
            if config is not None:
                self._update(
                    max_current_setting=config.max_current,
                    direct_work_mode=config.direct_work_mode,
                )
        elif kind not in (
            p.TRIGGER_MESSAGE_CONF,
            p.CHANGE_CONFIGURATION_CONF,
            p.REMOTE_START_CONF,
            p.REMOTE_STOP_CONF,
        ):
            _LOGGER.debug("Duosida %s: unhandled message %s", self.host, kind)

    def _new_transaction_id(self) -> int:
        # Positive int32, distinct from the cloud's small sequential ids.
        return int(time.time()) & 0x7FFFFFFF

    # --- sending --------------------------------------------------------------

    def _id(self) -> str:
        if self._device_id is None:
            raise DuosidaConnectionError("wallbox not connected")
        return self._device_id

    def _next_message_id(self) -> int:
        self._message_id = (self._message_id + 1) & 0x0FFFFFFF
        return self._message_id

    async def _send(self, build: Callable[[int], bytes]) -> int:
        if not self._ready.is_set() or self._writer is None:
            raise DuosidaConnectionError("wallbox not connected")
        async with self._send_lock:
            message_id = self._next_message_id()
            self._writer.write(build(message_id))
            await self._writer.drain()
            # The wallbox is slow (802.11b); do not burst messages.
            await asyncio.sleep(0.2)
        return message_id

    async def _reply(self, data: bytes) -> None:
        if self._writer is None:
            return
        async with self._send_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def _request(self, build: Callable[[int], bytes]) -> p.Frame:
        if not self._ready.is_set() or self._writer is None:
            raise DuosidaConnectionError("wallbox not connected")
        future: asyncio.Future[p.Frame] = asyncio.get_running_loop().create_future()
        async with self._send_lock:
            message_id = self._next_message_id()
            self._pending[message_id] = future
            self._writer.write(build(message_id))
            await self._writer.drain()
        try:
            return await asyncio.wait_for(future, COMMAND_TIMEOUT_S)
        except TimeoutError as err:
            raise DuosidaCommandError("wallbox did not answer in time") from err
        finally:
            self._pending.pop(message_id, None)

    # --- state ----------------------------------------------------------------

    def _update(self, **changes: object) -> None:
        if self._ready.is_set():
            changes.setdefault("last_seen", time.time())
        self._state = replace(self._state, **changes)
        if self._on_update is not None:
            self._on_update(self._state)
