"""Offline coverage for optional DB last-cycle consumption sensors."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorExtraStoredData,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_SENSORS, CONF_TYPE
from midea_ac_lan.config_flow import MideaLanOptionsFlowHandler
from midea_ac_lan.const import CONF_MODEL, CONF_SUBTYPE, DEVICES, DOMAIN
from midea_ac_lan.sensor import MideaCycleEstimateSensor, MideaSensor, async_setup_entry
from midealan.const import ProtocolVersion
from midealan.devices.db import DeviceAttributes as DBAttributes
from midealan.devices.db import MideaDBDevice


def test_water_sensor_exposes_the_device_estimate() -> None:
    """An opted-in verified washer exposes its last-cycle water estimate."""
    device = Mock()
    device.device_type = 0xDB
    device.device_id = 1
    device.name = "Offline washer"
    device.model = "38132406"
    device.subtype = 19504
    device.attributes = {"last_cycle_water_consumption": 45.8}
    device.get_attribute.side_effect = device.attributes.get
    hass = SimpleNamespace(data={DOMAIN: {DEVICES: {1: device}}})
    entry = SimpleNamespace(
        data={CONF_DEVICE_ID: 1},
        options={CONF_SENSORS: ["last_cycle_water_consumption"]},
    )
    add_entities = Mock()

    asyncio.run(async_setup_entry(hass, entry, add_entities))

    sensors = add_entities.call_args.args[0]
    assert len(sensors) == 1
    assert sensors[0].native_value == pytest.approx(45.8)
    assert sensors[0].native_unit_of_measurement == "L"


def test_energy_sensor_exposes_the_device_estimate() -> None:
    """An opted-in verified washer exposes its last-cycle energy estimate."""
    device = Mock()
    device.device_type = 0xDB
    device.device_id = 1
    device.name = "Offline washer"
    device.model = "38132406"
    device.subtype = 19504
    device.attributes = {"last_cycle_energy_consumption": 0.01}
    device.get_attribute.side_effect = device.attributes.get
    hass = SimpleNamespace(data={DOMAIN: {DEVICES: {1: device}}})
    entry = SimpleNamespace(
        data={CONF_DEVICE_ID: 1},
        options={CONF_SENSORS: ["last_cycle_energy_consumption"]},
    )
    add_entities = Mock()

    asyncio.run(async_setup_entry(hass, entry, add_entities))

    sensors = add_entities.call_args.args[0]
    assert len(sensors) == 1
    assert sensors[0].native_value == pytest.approx(0.01)
    assert sensors[0].native_unit_of_measurement == "kWh"


WATER = "last_cycle_water_consumption"
ENERGY = "last_cycle_energy_consumption"


def setup_sensors(
    model: str,
    subtype: int,
    attributes: dict,
    selected: list[str],
    device_type: int = 0xDB,
) -> list[MideaCycleEstimateSensor]:
    """Run the real platform setup using a small synthetic device."""
    device = Mock(device_type=device_type, device_id=1, name="Offline washer")
    device.model = model
    device.subtype = subtype
    device.attributes = attributes
    device.get_attribute.side_effect = attributes.get
    hass = SimpleNamespace(data={DOMAIN: {DEVICES: {1: device}}})
    entry = SimpleNamespace(data={CONF_DEVICE_ID: 1}, options={CONF_SENSORS: selected})
    add_entities = Mock()
    asyncio.run(async_setup_entry(hass, entry, add_entities))
    return cast("list[MideaCycleEstimateSensor]", add_entities.call_args.args[0])


@pytest.mark.parametrize(
    ("model", "subtype"),
    [("38132406", 13145), ("38131140", 19504), ("38131140", 13145), ("38132386", 0)],
)
def test_crossed_or_other_models_never_create_sensors(model: str, subtype: int) -> None:
    """Both model and subtype must match, even if attributes exist and are selected."""
    assert not setup_sensors(
        model,
        subtype,
        {WATER: 45.8, ENERGY: 0.01},
        [WATER, ENERGY],
    )


def test_other_device_type_does_not_get_washer_sensors() -> None:
    """Other appliance registries do not inherit DB sensors."""
    assert not setup_sensors(
        "38132406",
        19504,
        {WATER: 1, ENERGY: 2},
        [WATER, ENERGY],
        0xDC,
    )


@pytest.mark.parametrize("attributes", [{}, {WATER: 0.0}, {ENERGY: 0.0}])
def test_missing_attributes_are_skipped_independently(attributes: dict) -> None:
    """An older or partially supported library creates no invalid entities."""
    sensors = setup_sensors("38132406", 19504, attributes, [WATER, ENERGY])
    assert {sensor._entity_key for sensor in sensors} == set(attributes)
    assert all(sensor.native_value == pytest.approx(0.0) for sensor in sensors)


def test_unselected_sensors_are_not_created() -> None:
    """Both estimates require explicit opt-in."""
    assert not setup_sensors("38132406", 19504, {WATER: 45.8, ENERGY: 0.01}, [])


@pytest.mark.parametrize(
    ("key", "device_class"),
    [(WATER, SensorDeviceClass.WATER), (ENERGY, SensorDeviceClass.ENERGY)],
)
def test_metadata_is_not_cumulative(key: str, device_class: SensorDeviceClass) -> None:
    """Per-cycle values have no cumulative state class and start unknown."""
    sensor = setup_sensors("38132406", 19504, {key: None}, [key])[0]
    assert sensor.native_value is None
    assert sensor.state_class is None
    assert sensor.device_class == device_class
    assert sensor._config["required_attribute"] == key


def test_last_cycle_value_remains_visible_offline() -> None:
    """A previously reported cycle is still visible when the device disconnects."""
    sensor = setup_sensors("38132406", 19504, {WATER: 45.8}, [WATER])[0]
    cast("Mock", sensor._device).available = False
    assert sensor.available
    assert sensor.native_value == pytest.approx(45.8)
    assert sensor.extra_state_attributes["value_source"] == "current_session"
    assert sensor.extra_state_attributes["device_connected"] is False


def test_restores_last_cycle_value_after_restart() -> None:
    """Restored native values remain visible and are labeled as restored."""
    sensor = setup_sensors("38132406", 19504, {WATER: None}, [WATER])[0]
    cast("Mock", sensor._device).available = False
    assert isinstance(sensor, RestoreSensor)
    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            sensor,
            "async_get_last_sensor_data",
            new_callable=AsyncMock,
            return_value=SensorExtraStoredData(45.8, "L"),
        ),
    ):
        asyncio.run(sensor.async_added_to_hass())
    assert sensor.available
    assert sensor.native_value == pytest.approx(45.8)
    assert sensor.extra_state_attributes["value_source"] == "restored"


def test_restore_sensor_native_data_round_trip() -> None:
    """Round-trip real RestoreSensor extra data as HA's storage would."""
    original = setup_sensors("38132406", 19504, {ENERGY: 0.01}, [ENERGY])[0]
    persisted = original.extra_restore_state_data.as_dict()
    restored = setup_sensors("38132406", 19504, {ENERGY: None}, [ENERGY])[0]
    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            restored,
            "async_get_last_extra_data",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(as_dict=lambda: persisted),
        ),
    ):
        asyncio.run(restored.async_added_to_hass())
    assert restored.native_value == pytest.approx(0.01)
    assert restored.extra_state_attributes["value_source"] == "restored"


def test_restored_value_is_replaced_by_new_report() -> None:
    """A fresh SDK report takes precedence over the old restored estimate."""
    attributes: dict[str, float | None] = {WATER: None}
    sensor = setup_sensors("38132406", 19504, attributes, [WATER])[0]
    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            sensor,
            "async_get_last_sensor_data",
            new_callable=AsyncMock,
            return_value=SensorExtraStoredData(45.8, "L"),
        ),
    ):
        asyncio.run(sensor.async_added_to_hass())
    attributes[WATER] = 0.0
    assert sensor.native_value == pytest.approx(0.0)
    assert sensor.extra_state_attributes["value_source"] == "current_session"
    attributes[WATER] = None
    cast("Mock", sensor._device).available = False
    assert sensor.native_value == pytest.approx(0.0)
    assert sensor.available


@pytest.mark.parametrize(
    "stored",
    [
        None,
        SensorExtraStoredData(42, "kWh"),
        SensorExtraStoredData(-1, "L"),
        SensorExtraStoredData(float("nan"), "L"),
    ],
)
def test_invalid_restore_data_does_not_invent_a_value(
    stored: SensorExtraStoredData | None,
) -> None:
    """Missing, mismatched or invalid saved values remain unknown."""
    sensor = setup_sensors("38132406", 19504, {WATER: None}, [WATER])[0]
    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            sensor,
            "async_get_last_sensor_data",
            new_callable=AsyncMock,
            return_value=stored,
        ),
    ):
        asyncio.run(sensor.async_added_to_hass())
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["value_source"] == "unknown"


def test_sensor_updates_follow_device_value() -> None:
    """An entity reads changed cycle values and never accumulates them."""
    attributes = {WATER: 45.8}
    sensor = setup_sensors("38132406", 19504, attributes, [WATER])[0]
    attributes[WATER] = 12.0
    assert sensor.native_value == pytest.approx(12.0)


@pytest.mark.parametrize("value", [None, 0.0, 45.8])
def test_options_show_only_known_values(value: float | None) -> None:
    """The options flow exposes an unselected sensor once a value is known."""
    device = Mock()
    device.attributes = {WATER: value}
    device.get_attribute.side_effect = device.attributes.get
    entry = SimpleNamespace(
        data={
            CONF_DEVICE_ID: 1,
            CONF_TYPE: 0xDB,
            CONF_MODEL: "38132406",
            CONF_SUBTYPE: 19504,
        },
        options={},
    )
    handler = MideaLanOptionsFlowHandler(entry)
    handler.hass = SimpleNamespace(data={DOMAIN: {DEVICES: {1: device}}})
    result = asyncio.run(handler.async_step_init())
    sensor_field = next(
        validator
        for field, validator in result["data_schema"].schema.items()
        if field.schema == CONF_SENSORS
    )
    assert (WATER in sensor_field.options) == (value is not None)


@pytest.mark.skipif(
    not hasattr(DBAttributes, "last_cycle_water_consumption"),
    reason="Pinned legacy library has no last-cycle decoder",
)
def test_real_library_decoder_feeds_integration_sensors() -> None:
    """Decode a synthetic minimal 0405 message through the actual SDK and entities."""
    device = MideaDBDevice(
        customize="",
        name="Offline washer",
        device_id=1,
        ip_address="127.0.0.1",
        port=6444,
        token="",
        key="",
        device_protocol=ProtocolVersion.V2,
        model="38132406",
        subtype=19504,
    )
    hass = SimpleNamespace(data={DOMAIN: {DEVICES: {1: device}}})
    entry = SimpleNamespace(
        data={CONF_DEVICE_ID: 1},
        options={CONF_SENSORS: [WATER, ENERGY]},
    )
    add_entities = Mock()
    asyncio.run(async_setup_entry(hass, entry, add_entities))
    sensors = {sensor._entity_key: sensor for sensor in add_entities.call_args.args[0]}
    assert sensors[WATER].native_value is None
    assert sensors[ENERGY].native_value is None
    # Minimal synthetic frame, not a representation of the real full capture.
    header = bytearray(10)
    header[0], header[2], header[9] = 0xAA, 0xDB, 0x04
    message = bytes(header + bytearray([0x05, 0xCA, 0x01, 0x0A, 0x00, 0x00]))
    device.process_message(message)
    assert sensors[WATER].native_value == pytest.approx(45.8)
    assert sensors[ENERGY].native_value == pytest.approx(0.01)


@pytest.mark.parametrize("locale", ["en", "pt-BR"])
def test_estimates_have_localized_names(locale: str) -> None:
    """Both entities have localized names that explicitly say estimated."""
    root = Path(__file__).resolve().parents[1]
    path = (
        root / "custom_components" / "midea_ac_lan" / "translations" / f"{locale}.json"
    )
    translations = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
    marker = "estimated" if locale == "en" else "estimado"
    for key in (WATER, ENERGY):
        assert marker in translations[key]["name"].lower()
