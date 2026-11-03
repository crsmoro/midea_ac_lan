"""Offline coverage for optional DB last-cycle consumption sensors."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_SENSORS, CONF_TYPE
from midea_ac_lan import device_selector
from midea_ac_lan.config_flow import MideaLanOptionsFlowHandler
from midea_ac_lan.const import CONF_MODEL, CONF_SUBTYPE, DEVICES, DOMAIN
from midea_ac_lan.midea_devices import MIDEA_DEVICES
from midea_ac_lan.sensor import MideaSensor, async_setup_entry
from midealan.const import ProtocolVersion


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

    sensors = {sensor._entity_key: sensor for sensor in add_entities.call_args.args[0]}
    assert sensors[WATER].native_value == pytest.approx(45.8)
    assert sensors[WATER].native_unit_of_measurement == "L"
    assert sensors[WATER_TOTAL].native_value == 0


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

    sensors = {sensor._entity_key: sensor for sensor in add_entities.call_args.args[0]}
    assert sensors[ENERGY].native_value == pytest.approx(0.01)
    assert sensors[ENERGY].native_unit_of_measurement == "kWh"
    assert sensors[ENERGY_TOTAL].native_value == 0


WATER = "last_cycle_water_consumption"
ENERGY = "last_cycle_energy_consumption"
WATER_TOTAL = "db_total_water_consumption"
ENERGY_TOTAL = "db_total_energy_consumption"
REPORT_SEQUENCE = "cycle_report_sequence"


def setup_sensors(
    model: str,
    subtype: int,
    attributes: dict,
    selected: list[str],
    device_type: int = 0xDB,
) -> list[MideaSensor]:
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
    return cast("list[MideaSensor]", add_entities.call_args.args[0])


def test_cumulative_cycle_sensors_are_energy_dashboard_compatible() -> None:
    """Supported cumulative usage entities are created and use increasing totals."""
    sensors = setup_sensors(
        "38132406",
        19504,
        {WATER: None, ENERGY: None, REPORT_SEQUENCE: 0},
        [],
    )
    by_key = {sensor._entity_key: sensor for sensor in sensors}

    assert set(by_key) == {WATER_TOTAL, ENERGY_TOTAL}
    assert by_key[WATER_TOTAL].native_value == 0
    assert by_key[WATER_TOTAL].device_class == SensorDeviceClass.WATER
    assert by_key[WATER_TOTAL].native_unit_of_measurement == "L"
    assert by_key[WATER_TOTAL].state_class == SensorStateClass.TOTAL_INCREASING
    assert by_key[ENERGY_TOTAL].native_value == 0
    assert by_key[ENERGY_TOTAL].device_class == SensorDeviceClass.ENERGY
    assert by_key[ENERGY_TOTAL].native_unit_of_measurement == "kWh"
    assert by_key[ENERGY_TOTAL].state_class == SensorStateClass.TOTAL_INCREASING


def test_cycle_totals_count_each_new_report_once() -> None:
    """Totals add every report event, even when the next estimate is unchanged."""
    attributes = {WATER: 45.8, ENERGY: 0.01, REPORT_SEQUENCE: 0}
    sensors = setup_sensors("38132406", 19504, attributes, [])
    by_key = {sensor._entity_key: sensor for sensor in sensors}
    water_total = by_key[WATER_TOTAL]
    energy_total = by_key[ENERGY_TOTAL]
    for sensor in by_key.values():
        sensor.hass = SimpleNamespace()
        sensor.schedule_update_if_running = Mock()

    water_total.update_state({REPORT_SEQUENCE: 1})
    energy_total.update_state({REPORT_SEQUENCE: 1})
    assert water_total.native_value == pytest.approx(45.8)
    assert energy_total.native_value == pytest.approx(0.01)

    water_total.update_state({REPORT_SEQUENCE: 1})
    energy_total.update_state({REPORT_SEQUENCE: 1})
    assert water_total.native_value == pytest.approx(45.8)
    assert energy_total.native_value == pytest.approx(0.01)

    attributes[REPORT_SEQUENCE] = 2
    water_total.update_state({REPORT_SEQUENCE: 2})
    energy_total.update_state({REPORT_SEQUENCE: 2})
    assert water_total.native_value == pytest.approx(91.6)
    assert energy_total.native_value == pytest.approx(0.02)


def test_cycle_total_restores_its_value_after_restart() -> None:
    """The cumulative sensor restores a valid saved native total."""
    sensors = setup_sensors(
        "38132406",
        19504,
        {ENERGY: None, REPORT_SEQUENCE: 0},
        [],
    )
    energy_total = next(
        sensor for sensor in sensors if sensor._entity_key == ENERGY_TOTAL
    )
    assert isinstance(energy_total, RestoreSensor)
    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            energy_total,
            "async_get_last_sensor_data",
            new_callable=AsyncMock,
            return_value=SensorExtraStoredData(1.23, "kWh"),
        ),
    ):
        asyncio.run(energy_total.async_added_to_hass())

    assert energy_total.native_value == pytest.approx(1.23)


def test_report_during_state_restore_keeps_restored_total() -> None:
    """A new cycle arriving while HA restores state is not overwritten."""
    attributes = {WATER: 45.8, ENERGY: None, REPORT_SEQUENCE: 1}
    sensors = setup_sensors("38132406", 19504, attributes, [])
    water_total = next(
        sensor for sensor in sensors if sensor._entity_key == WATER_TOTAL
    )
    water_total.hass = SimpleNamespace()
    water_total.schedule_update_if_running = Mock()

    async def load_state_with_report() -> SensorExtraStoredData:
        water_total.update_state({REPORT_SEQUENCE: 1})
        return SensorExtraStoredData(10.0, "L")

    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            water_total,
            "async_get_last_sensor_data",
            new_callable=AsyncMock,
            side_effect=load_state_with_report,
        ),
    ):
        asyncio.run(water_total.async_added_to_hass())

    assert water_total.native_value == pytest.approx(55.8)


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
    expected = set(attributes)
    if WATER in attributes:
        expected.add(WATER_TOTAL)
    if ENERGY in attributes:
        expected.add(ENERGY_TOTAL)
    assert {sensor._entity_key for sensor in sensors} == expected
    assert all(sensor.native_value == pytest.approx(0.0) for sensor in sensors)


def test_cumulative_totals_do_not_require_opt_in() -> None:
    """Default cumulative sensors exist even when last-cycle details are unselected."""
    sensors = setup_sensors("38132406", 19504, {WATER: 45.8, ENERGY: 0.01}, [])
    assert {sensor._entity_key for sensor in sensors} == {WATER_TOTAL, ENERGY_TOTAL}


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


@pytest.mark.parametrize("key", [WATER, ENERGY])
def test_unknown_cycle_value_remains_available_offline(key: str) -> None:
    """Before any report an offline device has an unknown, not unavailable state."""
    sensor = setup_sensors("38132406", 19504, {key: None}, [key])[0]
    cast("Mock", sensor._device).available = False
    assert sensor.native_value is None
    assert sensor.available is True
    assert sensor.extra_state_attributes == {
        "value_source": "unknown",
        "device_connected": False,
    }


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


def test_reenabled_sensor_restores_its_previous_value() -> None:
    """An opted-out then re-enabled sensor recovers saved HA native data."""
    original = setup_sensors("38132406", 19504, {WATER: 45.8}, [WATER])[0]
    persisted = original.extra_restore_state_data.as_dict()
    assert not any(
        sensor._entity_key == WATER
        for sensor in setup_sensors("38132406", 19504, {WATER: None}, [])
    )
    sensor_again = next(
        sensor
        for sensor in setup_sensors("38132406", 19504, {WATER: None}, [WATER])
        if sensor._entity_key == WATER
    )
    with (
        patch.object(MideaSensor, "async_added_to_hass", new_callable=AsyncMock),
        patch.object(
            sensor_again,
            "async_get_last_extra_data",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(as_dict=lambda: persisted),
        ),
    ):
        asyncio.run(sensor_again.async_added_to_hass())
    assert sensor_again.native_value == pytest.approx(45.8)
    assert sensor_again.extra_state_attributes["value_source"] == "restored"


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
def test_options_show_supported_values_even_before_first_cycle(
    value: float | None,
) -> None:
    """Allow supported sensors to be enabled before their first report."""
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
    assert WATER in sensor_field.options


def test_options_show_cycle_totals_before_first_report() -> None:
    """Cumulative sensors remain selectable before the first cycle report."""
    device = Mock()
    device.attributes = {WATER: None, ENERGY: None, REPORT_SEQUENCE: 0}
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

    assert {WATER_TOTAL, ENERGY_TOTAL} <= set(sensor_field.options)


def test_real_library_decoder_feeds_integration_sensors() -> None:
    """Decode a synthetic minimal 0405 message through the actual SDK and entities."""
    device = device_selector(
        device_type=0xDB,
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
    header[0], header[1], header[2], header[9] = 0xAA, 15, 0xDB, 0x04
    data = bytes(header) + bytes.fromhex("05 ca 01 0a 00")
    message = data + bytes([-sum(data[1:]) & 0xFF])
    device.process_message(message)
    assert sensors[WATER].native_value == pytest.approx(45.8)
    assert sensors[ENERGY].native_value == pytest.approx(0.01)


@pytest.mark.parametrize("locale", ["en", "pt-BR"])
def test_cycle_sensors_have_concise_english_names(locale: str) -> None:
    """Last-cycle labels are concise and all new cycle totals are named."""
    root = Path(__file__).resolve().parents[1]
    path = (
        root / "custom_components" / "midea_ac_lan" / "translations" / f"{locale}.json"
    )
    translations = json.loads(path.read_text(encoding="utf-8"))["entity"]["sensor"]
    assert translations[WATER]["name"] == "Water used in last cycle"
    assert translations[ENERGY]["name"] == "Energy used in last cycle"
    assert translations[WATER_TOTAL]["name"] == "Total water used"
    assert translations[ENERGY_TOTAL]["name"] == "Total energy used"
    entity_configs = cast(
        "dict[str, dict[str, Any]]",
        MIDEA_DEVICES[0xDB]["entities"],
    )
    assert entity_configs[WATER]["name"] == "Water used in last cycle"
    assert entity_configs[ENERGY]["name"] == "Energy used in last cycle"
