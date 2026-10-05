"""Protocol tests against frames captured from a DUOSIDA Mode3@32A (V2.5).

The device id is replaced by 19 zeros ending in 1; lengths are unchanged.
"""

from __future__ import annotations

import struct

import pytest

from conftest import protocol as p

DEVICE_ID = "0000000000000000001"
ID_HEX = DEVICE_ID.encode().hex()

# Captured 2026-10-05, device id sanitized.
TRIGGER_CONF = bytes.fromhex(f"9a03020800a20613{ID_HEX}a806c6af8240")
BOOT_NOTIFICATION = bytes.fromhex(
    "2262121144554f53494441204d6f646533403332411a13"
    + ID_HEX
    + "2205554348454e2a2d56322e3540303436326364622b3234312c4631332e77696669322c6d712c56312e3140653337623336662b373732003a00"
    + f"a20613{ID_HEX}a806c5af8240"
)
# StopTransactionReq after a remote stop (idTag DirectWorkUser, meter 101 Wh,
# transaction 2375227, reason 4 = Remote).
STOP_TRANSACTION_PAYLOAD = bytes.fromhex("0a100a0e446972656374576f726b5573657210651892a39bac0d20bbfc90012804")


def test_hello_is_trigger_boot_from_ios() -> None:
    frame = p.parse_frame(p.HELLO)
    assert frame.kind == p.TRIGGER_MESSAGE_REQ
    assert frame.client_id == "IOS"
    assert frame.message_id == 0


def test_identity() -> None:
    frame = p.parse_frame(BOOT_NOTIFICATION)
    assert frame.kind == p.BOOT_NOTIFICATION_REQ
    identity = p.parse_identity(frame.payload)
    assert identity.model == "DUOSIDA Mode3@32A"
    assert identity.device_id == DEVICE_ID
    assert identity.vendor == "UCHEN"
    assert identity.firmware.startswith("V2.5@")


def test_splitter_handles_concatenated_and_partial_frames() -> None:
    stream = TRIGGER_CONF + BOOT_NOTIFICATION
    splitter = p.FrameSplitter()
    assert splitter.feed(stream[:10]) == []
    frames = splitter.feed(stream[10:-3])
    assert frames == [TRIGGER_CONF]
    assert splitter.feed(stream[-3:]) == [BOOT_NOTIFICATION]


def test_splitter_overflow() -> None:
    splitter = p.FrameSplitter(max_buffer=8)
    with pytest.raises(p.ProtocolError):
        splitter.feed(p.field_bytes(1, b"x" * 20)[:15])


def test_stop_transaction() -> None:
    stop = p.parse_stop_transaction(STOP_TRANSACTION_PAYLOAD)
    assert stop.transaction_id == 2375227
    assert stop.meter_stop_wh == 101
    assert stop.reason == 4


def _sample(value: str, measurand: int, unit: int) -> bytes:
    return p.field_bytes(1, value.encode()) + p.field_varint(2, 4) + p.field_varint(4, measurand) + p.field_varint(7, unit)


def test_meter_values() -> None:
    meter_value = p.field_varint(1, p.zigzag(1791191288))
    for value, measurand, unit in (
        ("9.80", 14, 10), ("0.06", 1, 1), ("2232.85", 9, 4), ("57.00", 19, 12), ("227.80", 17, 11),
    ):
        meter_value += p.field_bytes(2, _sample(value, measurand, unit))
    payload = p.field_varint(1, 1) + p.field_varint(2, 2375227) + p.field_bytes(3, meter_value)
    meter = p.parse_meter_values(payload)
    assert meter.transaction_id == 2375227
    assert meter.current == pytest.approx(9.8)
    assert meter.energy_register == pytest.approx(0.06)
    assert meter.power == pytest.approx(2232.85)
    assert meter.temperature == pytest.approx(57.0)
    assert meter.voltage == pytest.approx(227.8)
    assert meter.session_energy is None


def test_status_notification() -> None:
    payload = p.field_varint(1, 1) + p.field_varint(2, 6) + p.field_bytes(3, b"") + p.field_varint(4, 2)
    status = p.parse_status_notification(payload)
    assert status.status == "charging"
    assert status.error_code == "no_error"


def test_status_defaults_to_available() -> None:
    # Protobuf omits zero values: no field 4 means Available.
    payload = p.field_varint(1, 1) + p.field_varint(2, 6)
    assert p.parse_status_notification(payload).status == "available"


def test_conf_status() -> None:
    assert p.parse_conf_status(b"") == "Accepted"
    assert p.parse_conf_status(p.field_varint(1, 1)) == "Rejected"


def test_configuration_hides_passwords() -> None:
    pulled = (
        bytes([3 << 3 | 5]) + struct.pack("<f", 6.0)  # field 3, fixed32 float
        + p.field_bytes(8, b"IOT")
        + p.field_bytes(9, b"secret")
        + p.field_varint(10, 1)
        + p.field_varint(12, 1)
    )
    payload = p.field_varint(1, 0) + p.field_bytes(15, pulled)
    config = p.parse_data_transfer_conf(payload)
    assert config is not None
    assert config.max_current == 6.0
    assert config.direct_work_mode is True
    assert config.connect_server is True
    assert 9 not in config.raw
    assert b"secret" not in repr(config).encode()


def test_set_max_current_bytes() -> None:
    raw = p.build_set_max_current(DEVICE_ID, 5, 16)
    frame = p.parse_frame(raw)
    assert frame.kind == p.CHANGE_CONFIGURATION_REQ
    assert frame.message_id == 5
    fields = p.decode(frame.payload)
    assert p.first(fields, 1) == b"VendorMaxWorkCurrent"
    assert p.first(fields, 2) == b"16"


@pytest.mark.parametrize("amps", [5, 33])
def test_set_max_current_range(amps: int) -> None:
    with pytest.raises(ValueError):
        p.build_set_max_current(DEVICE_ID, 1, amps)


def test_start_transaction_conf_accepts_explicitly() -> None:
    frame = p.parse_frame(p.build_start_transaction_conf(DEVICE_ID, 9, 1791191288))
    assert frame.kind == p.START_TRANSACTION_CONF
    fields = p.decode(frame.payload)
    id_tag_info = p.decode(p.first(fields, 1))
    assert id_tag_info == [(3, 0, 0)]  # status present and Accepted
    assert p.first(fields, 2) == 1791191288


def test_zigzag_roundtrip() -> None:
    for value in (0, 1, -1, 1791191241, -123456):
        assert p.unzigzag(p.zigzag(value)) == value
    # Captured BootNotificationConf time used by other projects: 2025-11-17.
    assert p.unzigzag(3526800158) == 1763400079
