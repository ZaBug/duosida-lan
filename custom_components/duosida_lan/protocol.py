"""Duosida wallbox local protocol: OCPP 1.6 messages encoded as Protobuf over TCP.

Every outer message is one OCPP message type (a oneof field) followed by
field 100 (client / device id) and field 101 (message id). Answers echo the
message id of the request. There is no length prefix: a message ends with
field 101.

This module is pure (no I/O, no Home Assistant imports) so it can be tested
on its own. Field numbers come from live captures on a DUOSIDA Mode3@32A
(UCHEN firmware V2.5) and the public projects listed in the README.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import struct

DEFAULT_PORT = 9988

# TriggerMessage(BootNotification, connector 0), client id "IOS", message id 0.
# Byte-identical to the opening message the vendor app sends; the wallbox
# answers with a BootNotificationReq that carries its identity.
HELLO = bytes.fromhex("a2030408001000a20603494f53a80600")

VENDOR_DOMAIN = b"smartchargepile.x-cheng.com"
REMOTE_ID_TAG = "XC_Remote_Tag"
MAX_CURRENT_KEY = "VendorMaxWorkCurrent"
DIRECT_WORK_MODE_KEY = "VendorDirectWorkMode"  # plug and charge, "1" / "0"

# Outer oneof field numbers (OCPP message types).
BOOT_NOTIFICATION_REQ = 4
CHANGE_CONFIGURATION_CONF = 9
CHANGE_CONFIGURATION_REQ = 10
DATA_TRANSFER_CONF = 15
DATA_TRANSFER_REQ = 16
HEARTBEAT_CONF = 29
HEARTBEAT_REQ = 30
METER_VALUES_CONF = 31
METER_VALUES_REQ = 32
REMOTE_START_CONF = 33
REMOTE_START_REQ = 34
REMOTE_STOP_CONF = 35
REMOTE_STOP_REQ = 36
START_TRANSACTION_CONF = 45
START_TRANSACTION_REQ = 46
STATUS_NOTIFICATION_CONF = 47
STATUS_NOTIFICATION_REQ = 48
STOP_TRANSACTION_CONF = 49
STOP_TRANSACTION_REQ = 50
TRIGGER_MESSAGE_CONF = 51
TRIGGER_MESSAGE_REQ = 52

FIELD_CLIENT_ID = 100
FIELD_MESSAGE_ID = 101

# TriggerMessage requestedMessage values.
TRIGGER_BOOT = 0
TRIGGER_METER_VALUES = 4
TRIGGER_STATUS = 5

CHARGE_POINT_STATUS = {
    0: "available",
    1: "preparing",
    2: "charging",
    3: "suspended_evse",
    4: "suspended_ev",
    5: "finishing",
    6: "reserved",
    7: "unavailable",
    8: "faulted",
}

CHARGE_POINT_ERROR = {
    0: "connector_lock_failure",
    1: "ev_communication_error",
    2: "ground_failure",
    3: "high_temperature",
    4: "internal_error",
    5: "local_list_conflict",
    6: "no_error",
    7: "other_error",
    8: "over_current_failure",
    9: "power_meter_failure",
    10: "power_switch_failure",
    11: "reader_failure",
    12: "reset_failure",
    13: "under_voltage",
    14: "over_voltage",
    15: "weak_signal",
}

# Generic OCPP status values used by the *.conf messages.
CONF_STATUS = {0: "Accepted", 1: "Rejected", 2: "RebootRequired", 3: "NotSupported"}

# Meter value measurand codes, as observed on the wire (value / unit).
MEASURAND_ENERGY_REGISTER = 1  # Energy.Active.Import.Register, kWh
MEASURAND_ENERGY_INTERVAL = 5  # Energy.Active.Import.Interval, kWh (session)
MEASURAND_POWER = 9  # Power.Active.Import, W
MEASURAND_CURRENT = 14  # Current.Import, A
MEASURAND_VOLTAGE = 17  # Voltage, V
MEASURAND_TEMPERATURE = 19  # Temperature, degC


class ProtocolError(ValueError):
    """A frame could not be decoded."""


# --- Protobuf primitives ----------------------------------------------------


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | 2) + encode_varint(len(value)) + value


def read_varint(buf: bytes | bytearray, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise IndexError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ProtocolError("varint too long")


def _skip(buf: bytes | bytearray, pos: int, wire: int) -> tuple[object, int]:
    if wire == 0:
        return read_varint(buf, pos)
    if wire == 1:
        end = pos + 8
    elif wire == 2:
        length, pos = read_varint(buf, pos)
        end = pos + length
    elif wire == 5:
        end = pos + 4
    else:
        raise ProtocolError(f"unsupported wire type {wire}")
    if end > len(buf):
        raise IndexError("truncated field")
    return bytes(buf[pos:end]), end


def decode(buf: bytes) -> list[tuple[int, int, object]]:
    """Decode one message into (field number, wire type, value) tuples."""
    fields: list[tuple[int, int, object]] = []
    pos = 0
    try:
        while pos < len(buf):
            key, pos = read_varint(buf, pos)
            value, pos = _skip(buf, pos, key & 7)
            fields.append((key >> 3, key & 7, value))
    except IndexError as err:
        raise ProtocolError(str(err)) from err
    return fields


def first(fields: list[tuple[int, int, object]], number: int, wire: int | None = None) -> object | None:
    for num, w, value in fields:
        if num == number and (wire is None or w == wire):
            return value
    return None


def as_text(value: object | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def as_float32(value: object | None) -> float | None:
    if isinstance(value, bytes) and len(value) == 4:
        return struct.unpack("<f", value)[0]
    return None


class FrameSplitter:
    """Cut a TCP byte stream into outer messages (each ends with field 101)."""

    def __init__(self, max_buffer: int = 65536) -> None:
        self._buffer = bytearray()
        self._max_buffer = max_buffer

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buffer.extend(chunk)
        frames: list[bytes] = []
        pos = start = 0
        try:
            while pos < len(self._buffer):
                key, pos = read_varint(self._buffer, pos)
                _, pos = _skip(self._buffer, pos, key & 7)
                if key >> 3 == FIELD_MESSAGE_ID:
                    frames.append(bytes(self._buffer[start:pos]))
                    start = pos
        except IndexError:
            pass
        del self._buffer[:start]
        if len(self._buffer) > self._max_buffer:
            self._buffer.clear()
            raise ProtocolError("frame buffer overflow")
        return frames

    def clear(self) -> None:
        self._buffer.clear()


# --- Message model ------------------------------------------------------------


@dataclass(slots=True)
class Frame:
    """One decoded outer message."""

    kind: int | None
    payload: bytes
    client_id: str | None
    message_id: int | None


def parse_frame(raw: bytes) -> Frame:
    kind: int | None = None
    payload = b""
    client_id: str | None = None
    message_id: int | None = None
    for number, wire, value in decode(raw):
        if number == FIELD_CLIENT_ID and wire == 2:
            client_id = as_text(value)
        elif number == FIELD_MESSAGE_ID and wire == 0:
            message_id = value  # type: ignore[assignment]
        elif kind is None and wire == 2:
            kind = number
            payload = value  # type: ignore[assignment]
    return Frame(kind, payload, client_id, message_id)


@dataclass(frozen=True, slots=True)
class Identity:
    model: str
    device_id: str
    vendor: str
    firmware: str


@dataclass(frozen=True, slots=True)
class StatusNotification:
    status: str
    error_code: str
    info: str
    vendor_error_code: str


@dataclass(slots=True)
class MeterValues:
    transaction_id: int | None = None
    current: float | None = None
    voltage: float | None = None
    power: float | None = None
    temperature: float | None = None
    session_energy: float | None = None
    energy_register: float | None = None


@dataclass(frozen=True, slots=True)
class StartTransaction:
    connector_id: int
    id_tag: str
    meter_start_wh: int


@dataclass(frozen=True, slots=True)
class StopTransaction:
    transaction_id: int | None
    meter_stop_wh: int
    reason: int | None


@dataclass(slots=True)
class Configuration:
    max_current: float | None = None
    direct_work_mode: bool | None = None
    connect_server: bool | None = None
    raw: dict[int, object] = field(default_factory=dict)


def parse_identity(payload: bytes) -> Identity:
    fields = decode(payload)
    return Identity(
        model=as_text(first(fields, 2, 2)).strip(),
        device_id=as_text(first(fields, 3, 2)),
        vendor=as_text(first(fields, 4, 2)),
        firmware=as_text(first(fields, 5, 2)),
    )


def parse_status_notification(payload: bytes) -> StatusNotification:
    fields = decode(payload)
    status = first(fields, 4, 0)
    error = first(fields, 2, 0)
    return StatusNotification(
        status=CHARGE_POINT_STATUS.get(status or 0, f"unknown_{status}"),  # type: ignore[arg-type]
        error_code=CHARGE_POINT_ERROR.get(error or 0, f"unknown_{error}"),  # type: ignore[arg-type]
        info=as_text(first(fields, 3, 2)),
        vendor_error_code=as_text(first(fields, 7, 2)),
    )


_MEASURAND_ATTR = {
    MEASURAND_CURRENT: "current",
    MEASURAND_VOLTAGE: "voltage",
    MEASURAND_POWER: "power",
    MEASURAND_TEMPERATURE: "temperature",
    MEASURAND_ENERGY_INTERVAL: "session_energy",
    MEASURAND_ENERGY_REGISTER: "energy_register",
}


def parse_meter_values(payload: bytes) -> MeterValues:
    """MeterValuesReq: f1 connector, f2 transaction id, f3 meterValue.

    meterValue: f1 timestamp, f2 repeated sampledValue. sampledValue: f1 value
    (text), f4 measurand, f7 unit.
    """
    fields = decode(payload)
    result = MeterValues(transaction_id=first(fields, 2, 0))  # type: ignore[arg-type]
    for number, wire, value in fields:
        if number != 3 or wire != 2:
            continue
        for sub_number, sub_wire, sample in decode(value):  # type: ignore[arg-type]
            if sub_number != 2 or sub_wire != 2:
                continue
            sample_fields = decode(sample)  # type: ignore[arg-type]
            attr = _MEASURAND_ATTR.get(first(sample_fields, 4, 0))  # type: ignore[arg-type]
            if attr is None:
                continue
            try:
                setattr(result, attr, float(as_text(first(sample_fields, 1, 2))))
            except ValueError:
                continue
    return result


def parse_start_transaction(payload: bytes) -> StartTransaction:
    fields = decode(payload)
    tag = first(fields, 2, 2)
    id_tag = as_text(first(decode(tag), 1, 2)) if isinstance(tag, bytes) else ""
    return StartTransaction(
        connector_id=first(fields, 1, 0) or 0,  # type: ignore[arg-type]
        id_tag=id_tag,
        meter_start_wh=first(fields, 3, 0) or 0,  # type: ignore[arg-type]
    )


def parse_stop_transaction(payload: bytes) -> StopTransaction:
    fields = decode(payload)
    return StopTransaction(
        transaction_id=first(fields, 4, 0),  # type: ignore[arg-type]
        meter_stop_wh=first(fields, 2, 0) or 0,  # type: ignore[arg-type]
        reason=first(fields, 5, 0),  # type: ignore[arg-type]
    )


def parse_conf_status(payload: bytes) -> str:
    """Status of ChangeConfiguration / RemoteStart / RemoteStop confirmations.

    Protobuf omits default values, so an empty payload means 0 (Accepted).
    """
    status = first(decode(payload), 1, 0) or 0
    return CONF_STATUS.get(status, f"unknown_{status}")  # type: ignore[arg-type]


# Configuration fields in DataPullAllConfigurationConf (field 15 of DataTransferConf).
_SECRET_CONFIG_FIELDS = frozenset({7, 9})  # Wi-Fi AP and station passwords


def parse_data_transfer_conf(payload: bytes) -> Configuration | None:
    """Return the configuration if this DataTransferConf carries one."""
    pulled = first(decode(payload), 15, 2)
    if not isinstance(pulled, bytes):
        return None
    config = Configuration()
    for number, wire, value in decode(pulled):
        if number in _SECRET_CONFIG_FIELDS:
            continue
        config.raw[number] = value
        if number == 3 and wire == 5:
            config.max_current = as_float32(value)
        elif number == 10 and wire == 0:
            config.direct_work_mode = bool(value)
        elif number == 12 and wire == 0:
            config.connect_server = bool(value)
    return config


# --- Messages the integration sends -------------------------------------------


def _outer(kind: int, payload: bytes, client_id: str, message_id: int) -> bytes:
    return (
        field_bytes(kind, payload)
        + field_bytes(FIELD_CLIENT_ID, client_id.encode())
        + field_varint(FIELD_MESSAGE_ID, message_id)
    )


def build_trigger(client_id: str, message_id: int, requested: int) -> bytes:
    return _outer(TRIGGER_MESSAGE_REQ, field_varint(1, requested) + field_varint(2, 1), client_id, message_id)


def build_pull_configuration(client_id: str, message_id: int) -> bytes:
    inner = (
        field_bytes(1, VENDOR_DOMAIN)
        + field_bytes(2, b"DataPullAllConfigurationReq")
        + field_bytes(14, b"")
    )
    return _outer(DATA_TRANSFER_REQ, inner, client_id, message_id)


def build_change_configuration(client_id: str, message_id: int, key: str, value: str) -> bytes:
    payload = field_bytes(1, key.encode()) + field_bytes(2, value.encode())
    return _outer(CHANGE_CONFIGURATION_REQ, payload, client_id, message_id)


def build_set_max_current(client_id: str, message_id: int, amps: int) -> bytes:
    if not 6 <= amps <= 32:
        raise ValueError("current must be between 6 and 32 A")
    return build_change_configuration(client_id, message_id, MAX_CURRENT_KEY, str(amps))


def build_set_direct_work_mode(client_id: str, message_id: int, enabled: bool) -> bytes:
    # Verified live: "0" / "1" accepted, read back in configuration field 10.
    # (VendorLEDStrength is rejected locally for every value on firmware V2.5.)
    return build_change_configuration(client_id, message_id, DIRECT_WORK_MODE_KEY, "1" if enabled else "0")


def build_remote_start(client_id: str, message_id: int, id_tag: str = REMOTE_ID_TAG) -> bytes:
    # idTag as a plain string in field 2: tested live, answered Accepted.
    payload = field_varint(1, 1) + field_bytes(2, id_tag.encode())
    return _outer(REMOTE_START_REQ, payload, client_id, message_id)


def build_remote_stop(client_id: str, message_id: int, transaction_id: int) -> bytes:
    return _outer(REMOTE_STOP_REQ, field_varint(1, transaction_id), client_id, message_id)


def build_ack(kind: int, client_id: str, message_id: int) -> bytes:
    """Empty confirmation (StatusNotificationConf, MeterValuesConf, StopTransactionConf)."""
    return _outer(kind, b"", client_id, message_id)


def build_start_transaction_conf(client_id: str, message_id: int, transaction_id: int) -> bytes:
    # f1 idTagInfo {f3 status = 0 Accepted}, f2 transactionId. Field order
    # follows the OCPP schema, as in every message seen on the wire. The
    # status is written explicitly: with an empty idTagInfo the wallbox kept
    # resending StartTransactionReq every minute (live test, 2026-10-05).
    id_tag_info = field_varint(3, 0)
    payload = field_bytes(1, id_tag_info) + field_varint(2, transaction_id)
    return _outer(START_TRANSACTION_CONF, payload, client_id, message_id)


def build_heartbeat_conf(client_id: str, message_id: int, now: int) -> bytes:
    return _outer(HEARTBEAT_CONF, field_varint(1, zigzag(now)), client_id, message_id)


def build_vendor_status_conf(client_id: str, message_id: int) -> bytes:
    return _outer(DATA_TRANSFER_CONF, field_varint(1, 0) + field_bytes(11, b""), client_id, message_id)


def is_vendor_status(payload: bytes) -> bool:
    return as_text(first(decode(payload), 2, 2)) == "DataVendorStatusReq"
