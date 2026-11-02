"""Regression tests against the installed SDK, without appliance access."""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_IP_ADDRESS,
    CONF_NAME,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_TYPE,
)
from midea_ac_lan import async_setup_entry, device_selector
from midea_ac_lan.const import (
    CONF_MODEL,
    CONF_SUBTYPE,
    DEVICES,
    DOMAIN,
)
from midea_ac_lan.db_cycle_adapter import ENERGY, WATER, CycleEstimateDBDevice
from midealan.const import DeviceType, ProtocolVersion
from midealan.devices import device_selector as sdk_device_selector
from midealan.devices.db import MideaDBDevice
from midealan.message import MessageQuestCustom, MessageResponse, MessageType


def args(
    device_type: int = 0xDB,
    model: str = "38132406",
    subtype: int = 19504,
) -> dict[str, Any]:
    """Return a local, disconnected SDK device configuration."""
    return {
        "name": "washer",
        "device_id": 1,
        "device_type": device_type,
        "ip_address": "127.0.0.1",
        "port": 6444,
        "token": "",
        "key": "",
        "device_protocol": ProtocolVersion.V2,
        "model": model,
        "subtype": subtype,
        "customize": "",
        "mac": None,
        "serial_number": None,
        "sn8": None,
        "manufacturer_code": None,
        "cloud_name": None,
    }


def frame(body: bytes, message_type: int = 4) -> bytes:
    """Construct a synthetic SDK-format frame, not a full device capture."""
    header = bytearray(10)
    header[0], header[1], header[2], header[9] = (
        0xAA,
        10 + len(body),
        0xDB,
        message_type,
    )
    data = bytes(header) + body
    return data + bytes([-sum(data[1:]) & 0xFF])


def test_rejects_report_with_zero_declared_length() -> None:
    """The previously accepted malformed report cannot change saved estimates."""
    device = device_selector(**args())
    device.process_message(frame(bytes.fromhex("05 34 12 78 56")))
    before = device.attributes.copy()
    malformed = bytes.fromhex("aa00db0000000000000405ca010a0000")
    assert device.process_message(malformed) == {}
    assert device.attributes == before


def test_rejects_report_with_invalid_checksum() -> None:
    """A complete frame with a corrupted checksum must not update state."""
    device = device_selector(**args())
    device.process_message(frame(bytes.fromhex("05 34 12 78 56")))
    before = device.attributes.copy()
    message = bytearray(frame(bytes.fromhex("05 ca 01 0a 00")))
    message[-1] ^= 1
    assert device.process_message(bytes(message)) == {}
    assert device.attributes == before


def test_synthetic_frame_matches_sdk_serializer() -> None:
    """The fixture follows the SDK's actual length and checksum convention."""
    body = bytes.fromhex("05 ca 01 0a 00")
    expected = MessageQuestCustom(
        DeviceType.DB,
        0,
        MessageType.notify1,
        bytearray(body),
    ).serialize()
    assert frame(body) == bytes(expected)


@pytest.mark.parametrize("declared_length", [0, 10, 11, 12, 13, 14, 16, 255])
def test_rejects_invalid_declared_length(declared_length: int) -> None:
    """Reject even a checksum-consistent short frame with extra trailing bytes."""
    device = device_selector(**args())
    device.process_message(frame(bytes.fromhex("05 34 12 78 56")))
    before = device.attributes.copy()
    message = bytearray(frame(bytes.fromhex("05 ca 01 0a 00")))
    message[1] = declared_length
    if MessageResponse.HEADER_LENGTH + 1 <= declared_length < len(message) - 1:
        message[declared_length] = -sum(message[1:declared_length]) & 0xFF
    assert device.process_message(bytes(message)) == {}
    assert device.attributes == before


@pytest.mark.parametrize("trailing", [b"\x10" * 16, b"\x00" * 16, b"\x12\x34\xb7"])
def test_rejects_bytes_beyond_declared_frame(trailing: bytes) -> None:
    """The SDK removes AES padding before dispatching decrypted messages."""
    device = device_selector(**args())
    message = frame(bytes.fromhex("05 ca 01 0a 00")) + trailing
    assert device.process_message(message) == {}
    assert device.attributes[WATER] is None
    assert device.attributes[ENERGY] is None


def test_padding_cannot_hide_invalid_checksum() -> None:
    """An extra byte cannot substitute for the frame's checksum."""
    device = device_selector(**args())
    device.process_message(frame(bytes.fromhex("05 34 12 78 56")))
    before = device.attributes.copy()
    message = bytearray(frame(bytes.fromhex("05 ca 01 0a 00")))
    message[-1] ^= 1
    assert device.process_message(bytes(message) + b"\x10" * 16) == {}
    assert device.attributes == before


def test_extended_frame_checksum_covers_the_entire_declared_body() -> None:
    """Extra body bytes precede the checksum and are not AES padding."""
    device = device_selector(**args())
    message = frame(bytes.fromhex("05 ca 01 0a 00 12 34"))
    assert device.process_message(message) == {
        WATER: 45.8,
        ENERGY: 0.01,
    }
    before = device.attributes.copy()
    corrupted = bytearray(message)
    corrupted[-2] ^= 1
    assert device.process_message(bytes(corrupted)) == {}
    assert device.attributes == before


def test_unrelated_frame_still_delegated_without_new_validation() -> None:
    """The new checks apply to 0405 only, not legacy SDK messages."""
    device = device_selector(**args())
    message = bytearray(frame(bytes.fromhex("06 ca 01 0a 00")))
    message[1] = 0
    with patch.object(
        MideaDBDevice,
        "process_message",
        return_value={"legacy": 1},
    ) as sdk:
        assert device.process_message(bytes(message)) == {"legacy": 1}
    sdk.assert_called_once_with(bytes(message))


def test_toshiba_protocol_frames_are_delegated_to_sdk() -> None:
    """Toshiba frames must reach any protocol detection supplied by the SDK."""
    device = device_selector(**args())
    message = bytes.fromhex("55aacc33") + bytes(16)
    with patch.object(
        MideaDBDevice,
        "process_message",
        return_value={"legacy": 1},
    ) as sdk:
        assert device.process_message(message) == {"legacy": 1}
        sdk.assert_called_once_with(message)


@pytest.mark.parametrize("prefix", [bytes.fromhex("bb0fdb"), bytes.fromhex("aa0fac")])
def test_invalid_frame_identity_never_reaches_sdk_decoder(prefix: bytes) -> None:
    """Corrupt foreign frames must not bypass the 0405 checksum check."""
    device = device_selector(**args())
    message = prefix + bytes.fromhex("00000000000405ca010a0039")
    with patch.object(
        MideaDBDevice,
        "process_message",
        return_value={WATER: 45.8},
    ) as sdk:
        assert device.process_message(message) == {}
        sdk.assert_not_called()
    assert device.attributes[WATER] is None


def test_toshiba_mode_does_not_decode_db_consumption() -> None:
    """Future SDK protocol detection takes precedence over local DB decoding."""
    device = device_selector(**args())
    message = frame(bytes.fromhex("05 ca 01 0a 00"))
    with (
        patch.object(CycleEstimateDBDevice, "toshiba_protocol", True, create=True),
        patch.object(
            MideaDBDevice,
            "process_message",
            return_value={"legacy": 1},
        ) as sdk,
    ):
        assert device.process_message(message) == {"legacy": 1}
        sdk.assert_called_once_with(message)
    assert device.attributes[WATER] is None


def test_setup_selects_adapter_with_real_sdk_without_network() -> None:
    """Exercise the actual HA integration setup selection and store path."""
    config = args()
    entry = Mock()
    entry.data = {
        CONF_NAME: config["name"],
        CONF_DEVICE_ID: 1,
        CONF_TYPE: 0xDB,
        CONF_IP_ADDRESS: config["ip_address"],
        CONF_PORT: 6444,
        CONF_MODEL: config["model"],
        CONF_SUBTYPE: config["subtype"],
        CONF_PROTOCOL: ProtocolVersion.V2.value,
    }
    entry.options = {}
    hass = SimpleNamespace(
        data={DOMAIN: {DEVICES: {}}},
        bus=SimpleNamespace(async_listen_once=Mock(return_value=Mock())),
        config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()),
        async_add_import_executor_job=AsyncMock(side_effect=lambda fn, *a: fn(*a)),
    )
    with (
        patch.object(CycleEstimateDBDevice, "open") as opened,
        patch("midea_ac_lan._remove_unsupported_entities"),
    ):
        assert asyncio.run(async_setup_entry(hass, entry)) is True
    device = hass.data[DOMAIN][DEVICES][1]
    assert isinstance(device, CycleEstimateDBDevice)
    assert device.attributes[WATER] is None
    assert device.attributes[ENERGY] is None
    opened.assert_called_once()
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()


@pytest.mark.parametrize(
    "identity",
    [
        (0xDB, "38132406", 19503),
        (0xDB, "38132407", 19504),
        (0xDB, "", 19504),
        (0xAC, "38132406", 19504),
    ],
)
def test_other_profiles_use_unmodified_sdk(identity: tuple[int, str, int]) -> None:
    """Unverified profiles use the identical class and attributes as the SDK."""
    kwargs = args(*identity)
    actual = device_selector(**kwargs)
    expected = sdk_device_selector(**kwargs)
    assert type(actual) is type(expected)
    assert actual.attributes == expected.attributes
    assert not isinstance(actual, CycleEstimateDBDevice)


def test_normal_status_matches_sdk_and_retains_cycle_values() -> None:
    """Delegate ordinary status to the SDK without clearing the estimates."""
    body = bytes.fromhex(
        "030101001f0520010300030400400000204f0000db5933b10000002d08080000"
        "02ffff000000ffffffffffffffff140101ffffffff01ff200000080000",
    )
    adapted = device_selector(**args())
    original = sdk_device_selector(**args())
    adapted.process_message(frame(bytes.fromhex("05 ca 01 0a 00")))
    message = frame(body, 3)
    assert adapted.process_message(message) == original.process_message(message)
    assert adapted.attributes[WATER] == pytest.approx(45.8)
    assert adapted.attributes[ENERGY] == pytest.approx(0.01)


def test_sdk_update_callbacks_receive_decoded_values() -> None:
    """Use the real SDK's listener dispatch after decoding a report."""
    device = device_selector(**args())
    callback = Mock()
    device.register_update(callback)
    status = device.process_message(frame(bytes.fromhex("05 ca 01 0a 00")))
    device.update_all(status)
    callback.assert_called_once_with({WATER: 45.8, ENERGY: 0.01})


def test_decoder_preserves_sdk_status_and_handles_zero() -> None:
    """New reports replace old, including zero; ordinary DB queries still work."""
    device = device_selector(**args())
    status = device.process_message(frame(bytes.fromhex("05 ca 01 0a 00")))
    assert status == {WATER: 45.8, ENERGY: 0.01}
    assert device.attributes[WATER] == pytest.approx(45.8)
    zero = device.process_message(frame(bytes.fromhex("05 00 00 00 00")))
    assert zero == {WATER: 0, ENERGY: 0}
    assert device.attributes[ENERGY] == 0
    assert device.build_query()
    assert callable(device.set_attribute)
    assert device.process_message(frame(bytes.fromhex("05 34 12 78 56"))) == {
        WATER: 0x1234 / 10,
        ENERGY: 0x5678 / 1000,
    }


@pytest.mark.parametrize(
    "message",
    [
        b"",
        b"\xaa" * 9,
        frame(b""),
        frame(bytes.fromhex("05")),
        frame(bytes.fromhex("05 00")),
        frame(bytes.fromhex("05 00 00")),
        frame(bytes.fromhex("05 00 00 00")),
        frame(bytes.fromhex("05 ca 01 0a 00"), 255),
        frame(bytes.fromhex("05 ca 01 0a 00"), 2),
        frame(bytes.fromhex("05 ca 01 0a 00"), 3),
        frame(bytes.fromhex("05 ca 01 0a 00"), 5),
        frame(bytes.fromhex("06 ca 01 0a 00")),
    ],
)
def test_invalid_or_unrelated_messages_preserve_last_values(message: bytes) -> None:
    """Truncation, unrelated bodies and types must not alter estimates."""
    device = device_selector(**args())
    device.process_message(frame(bytes.fromhex("05 ca 01 0a 00")))
    status = device.process_message(message)
    assert WATER not in status
    assert ENERGY not in status
    assert device.attributes[WATER] == pytest.approx(45.8)
    assert device.attributes[ENERGY] == pytest.approx(0.01)
