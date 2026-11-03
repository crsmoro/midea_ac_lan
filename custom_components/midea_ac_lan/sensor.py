"""Sensor for Midea Lan."""

import logging
import math
import time
from datetime import timedelta
from typing import Any, cast

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICE_ID, CONF_SENSORS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import StateType
from midealan.device import MideaDevice
from midealan.devices.e1 import MideaE1Device

from .cloud import (
    MideaCloudCoordinator,
    create_cloud_coordinator,
)
from .const import DEVICES, DOMAIN, supports_device
from .db_cycle_adapter import CYCLE_REPORT_SEQUENCE
from .midea_devices import MIDEA_DEVICES
from .midea_entity import MideaEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for device."""
    device_id = config_entry.data.get(CONF_DEVICE_ID)
    device = hass.data[DOMAIN][DEVICES].get(device_id)
    extra_sensors = config_entry.options.get(CONF_SENSORS, [])
    sensors = []
    # Shared by all cloud usage sensors of the device; created lazily by the
    # first opted-in one and left None when no cloud account is stored.
    cloud_coordinator: MideaCloudCoordinator | None = None
    cloud_unconfigured = False
    for entity_key, config in cast(
        "dict",
        MIDEA_DEVICES[device.device_type]["entities"],
    ).items():
        if (
            config["type"] != Platform.SENSOR
            or not supports_device(device.model, device.subtype, config)
            or (not config.get("default") and entity_key not in extra_sensors)
        ):
            continue
        required_attribute = config.get("required_attribute")
        if (
            required_attribute is not None
            and required_attribute not in device.attributes
        ):
            continue
        if config.get("cloud_report") is not None:
            if cloud_coordinator is None and not cloud_unconfigured:
                cloud_coordinator = create_cloud_coordinator(
                    hass,
                    config_entry,
                    device_id,
                    device.name,
                )
                if cloud_coordinator is None:
                    cloud_unconfigured = True
                    _LOGGER.warning(
                        "Usage statistics sensors of device %s are selected, "
                        "but no Midea cloud account is stored in the "
                        "integration options; skipping them",
                        device_id,
                    )
            if cloud_coordinator is None:
                continue
            sensors.append(
                MideaCloudUsageSensor(device, entity_key, cloud_coordinator),
            )
        elif config.get("cycle_total"):
            sensors.append(MideaCumulativeCycleSensor(device, entity_key))
        elif config.get("cycle_estimate"):
            sensors.append(MideaCycleEstimateSensor(device, entity_key))
        elif config.get("estimate"):
            sensor = MideaEstimatedUsageSensor(device, entity_key)
            sensors.append(sensor)
        elif config.get("duration_from_minutes"):
            sensor = MideaMinuteDurationSensor(device, entity_key)
            sensors.append(sensor)
        elif config.get("state_from_flags"):
            sensor = MideaFlagStateSensor(device, entity_key)
            sensors.append(sensor)
        else:
            sensor = MideaSensor(device, entity_key)
            sensors.append(sensor)
    if cloud_coordinator is not None:
        # Prime the sensors with the current report; failures keep them
        # unavailable and are reported by the coordinator.
        await cloud_coordinator.async_refresh()
    async_add_entities(sensors)


class MideaSensor(MideaEntity, SensorEntity):
    """Represent a Midea sensor."""

    def __init__(self, device: MideaDevice, entity_key: str) -> None:
        """Initialize Midea sensor."""
        super().__init__(device, entity_key)
        # Timer configuration: "down" for countdown, "up" for countup
        self._timer = self._config.get("timer")
        self._timer_base_value: int | None = None
        self._timer_last_update: float | None = None
        self._timer_listener = None

    @property
    def native_value(self) -> StateType:
        """Native value of the sensor."""
        value = self._device.get_attribute(self._entity_key)
        if value in self._config.get("missing_values", []):
            return None
        # If an options mapping exists, translate the raw value to its enum key.
        options = self._config.get("options")
        if options is not None and isinstance(value, int | str):
            # For an enum sensor an unmapped value is not a valid state, so
            # report it as unknown (None) instead of leaking the raw int.
            return cast("StateType", options.get(value))

        # Timer mode: calculate elapsed time
        if self._timer and isinstance(value, int):
            now = time.time()
            if self._timer_last_update is not None:
                elapsed = int(now - self._timer_last_update)
                if self._timer == "down":
                    # Countdown: value decreases by elapsed seconds
                    return cast("StateType", max(0, value - elapsed))
                if self._timer == "up":
                    # Countup: value increases by elapsed seconds
                    return cast("StateType", value + elapsed)
            return cast("StateType", value)

        return cast("StateType", value)

    @property
    def device_class(self) -> SensorDeviceClass:
        """Device class of the sensor."""
        return cast("SensorDeviceClass", self._config.get("device_class"))

    @property
    def state_class(self) -> SensorStateClass | None:
        """State class of the sensor."""
        return cast("SensorStateClass | None", self._config.get("state_class"))

    @property
    def options(self) -> list[str] | None:
        """List of possible states for an enum sensor."""
        if self.device_class != SensorDeviceClass.ENUM:
            return None
        options = self._config.get("options")
        return list(options.values()) if options else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Unit of measurement for the sensor."""
        return cast("str | None", self._config.get("unit"))

    @property
    def suggested_display_precision(self) -> int | None:
        """Suggested number of decimal digits for display."""
        return cast("int | None", self._config.get("suggested_display_precision"))

    @property
    def suggested_unit_of_measurement(self) -> str | None:
        """Preferred display unit for device-class conversion."""
        return cast("str | None", self._config.get("suggested_unit"))

    @property
    def capability_attributes(self) -> dict[str, Any] | None:
        """Capability attributes of the sensor."""
        if self.options is not None:
            return {"options": self.options}
        return {"state_class": self.state_class} if self.state_class else {}

    async def async_added_to_hass(self) -> None:
        """Subscribe to device updates and start timer tracking."""
        await super().async_added_to_hass()
        # Start timer tracking if configured
        if self._timer:
            value = self._device.get_attribute(self._entity_key)
            if isinstance(value, int):
                self._timer_base_value = value
                self._timer_last_update = time.time()
            # Register 1-second interval update
            self._timer_listener = async_track_time_interval(
                self.hass,
                self._async_timer_update,
                timedelta(seconds=1),
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from device updates and stop timer tracking."""
        await super().async_will_remove_from_hass()
        if self._timer_listener:
            self._timer_listener()
            self._timer_listener = None

    @callback
    def update_state(self, status: Any) -> None:  # ruff:ignore[any-type]
        """Update entity state."""
        if self._timer and self._entity_key in status:
            value = self._device.get_attribute(self._entity_key)
            if isinstance(value, int):
                self._timer_base_value = value
                self._timer_last_update = time.time()
        super().update_state(status)

    @callback
    def _async_timer_update(self, _now: Any) -> None:  # ruff:ignore[any-type]
        """Update timer state every second."""
        self.schedule_update_ha_state()


class MideaCycleEstimateSensor(MideaSensor, RestoreSensor):
    """Keep a last-cycle estimate across disconnects and HA restarts."""

    def __init__(self, device: MideaDevice, entity_key: str) -> None:
        """Initialize with no value until a report or saved state is available."""
        super().__init__(device, entity_key)
        self._last_value: float | None = None
        self._restored = False

    async def async_added_to_hass(self) -> None:
        """Restore a compatible stored native value, if any."""
        await super().async_added_to_hass()
        stored = await self.async_get_last_sensor_data()
        if (
            stored is not None
            and stored.native_unit_of_measurement == self.native_unit_of_measurement
        ):
            value = stored.native_value
            if (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                self._last_value = float(value)
                self._restored = True

    @property
    def native_value(self) -> StateType:
        """Prefer the device's report, otherwise retain the last known value."""
        value = self._device.get_attribute(self._entity_key)
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        ):
            self._last_value = float(value)
            self._restored = False
        return self._last_value

    @property
    def available(self) -> bool:
        """An estimate can be unknown independently of device connectivity."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """Identify the origin and the appliance's current connectivity."""
        value = self.native_value
        return {
            "value_source": (
                "unknown"
                if value is None
                else "restored"
                if self._restored
                else "current_session"
            ),
            "device_connected": self._device.available,
        }


class MideaCumulativeCycleSensor(MideaSensor, RestoreSensor):
    """Accumulate per-cycle reports into a persistent total-increasing sensor."""

    _REPORT_SEQUENCE = CYCLE_REPORT_SEQUENCE

    def __init__(self, device: MideaDevice, entity_key: str) -> None:
        """Initialize the cumulative total and event cursor."""
        super().__init__(device, entity_key)
        self._total = 0.0
        self._last_report_sequence = 0
        self._source_attribute = cast("str", self._config["source_attribute"])
        self._restoring = False
        self._pending_report_values: list[float] = []

    async def async_added_to_hass(self) -> None:
        """Restore state and preserve reports arriving during async setup."""
        self._restoring = True
        await super().async_added_to_hass()
        stored = await self.async_get_last_sensor_data()
        if (
            stored is not None
            and stored.native_unit_of_measurement == self.native_unit_of_measurement
        ):
            value = stored.native_value
            if (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                self._total = float(value)
        pending = self._pending_report_values
        self._pending_report_values = []
        self._total += sum(pending)
        self._restoring = False
        sequence = self._device.get_attribute(CYCLE_REPORT_SEQUENCE)
        if isinstance(sequence, int) and not isinstance(sequence, bool):
            self._last_report_sequence = max(self._last_report_sequence, sequence)
        if pending and self.hass:
            self.schedule_update_if_running()

    @property
    def native_value(self) -> StateType:
        """Cumulative consumption reported by the appliance."""
        return round(self._total, 3)

    @property
    def available(self) -> bool:
        """The persisted total remains useful while the appliance is offline."""
        return True

    @callback
    def update_state(self, status: Any) -> None:  # ruff:ignore[any-type]
        """Add one new valid cycle report, ignoring repeated sequence callbacks."""
        sequence = status.get(self._REPORT_SEQUENCE)
        if (
            isinstance(sequence, int)
            and not isinstance(sequence, bool)
            and sequence > self._last_report_sequence
        ):
            value = self._device.get_attribute(self._source_attribute)
            if (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
                amount = float(value)
                if self._restoring:
                    self._pending_report_values.append(amount)
                else:
                    self._total += amount
            self._last_report_sequence = sequence
            if self.hass and not self._restoring:
                self.schedule_update_if_running()
        super().update_state(status)


class MideaCloudUsageSensor(MideaSensor):
    """Represent a usage statistic from the Midea cloud day report.

    The value does not come from the LAN device but from the cloud report
    coordinator, so availability follows the coordinator instead of the
    device connection.
    """

    def __init__(
        self,
        device: MideaDevice,
        entity_key: str,
        coordinator: MideaCloudCoordinator,
    ) -> None:
        """Initialize cloud usage sensor."""
        super().__init__(device, entity_key)
        self._coordinator = coordinator

    @property
    def native_value(self) -> StateType:
        """Newest value of the configured cloud report field."""
        report = self._coordinator.data
        if report is None:
            return None
        return cast(
            "StateType",
            getattr(report, cast("str", self._config["cloud_report"])),
        )

    @property
    def available(self) -> bool:
        """Whether the last cloud report fetch succeeded."""
        return bool(
            self._coordinator.last_update_success
            and self._coordinator.data is not None,
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Date the underlying cloud day report covers."""
        report = self._coordinator.data
        return (
            {}
            if report is None
            else {
                "report_date": report.report_date.isoformat(),
            }
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to device and coordinator updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(
                self._handle_coordinator_update,
            ),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write the refreshed cloud report value."""
        self.schedule_update_if_running()


class MideaMinuteDurationSensor(MideaSensor):
    """Present a whole-minute appliance duration as hours and minutes."""

    _HOUR_PRECISION = 6

    @property
    def native_value(self) -> StateType:
        """Convert source minutes to hours for HA's duration formatter."""
        raw_value = self._device.get_attribute(self._entity_key)
        if raw_value is None:
            return None
        minutes = max(0, int(cast("int", raw_value)))
        if minutes == 0:
            return 0

        # HA's duration formatter decomposes decimal hours with floor(). Round
        # upward below the appliance's one-minute resolution so values such as
        # 61 minutes cannot render as 1 hour 0 minutes through float truncation.
        scale = 10**self._HOUR_PRECISION
        return math.ceil((minutes / 60) * scale) / scale

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        """Keep the exact appliance value available for automations."""
        raw_value = self._device.get_attribute(self._entity_key)
        return {
            "remaining_minutes": max(0, int(cast("int", raw_value or 0))),
        }


class MideaFlagStateSensor(MideaSensor):
    """Derive one state from boolean attributes in configured priority order.

    The required ``state_from_flags`` config maps each flag attribute to its
    state string. The sensor reports the first set flag, even if multiple
    flags are set, and is unknown (None) while none of them is.
    This derived state does not enforce mutual exclusion on the device.
    """

    @property
    def native_value(self) -> StateType:
        """State of the first flag that is currently set."""
        for attribute, state in self._flags_to_state().items():
            if self._device.get_attribute(attribute):
                return state
        return None

    @property
    def options(self) -> list[str] | None:
        """States the sensor can report."""
        if self.device_class != SensorDeviceClass.ENUM:
            return None
        return list(self._flags_to_state().values())

    @callback
    def update_state(self, status: Any) -> None:  # ruff:ignore[any-type]
        """Update the state when any of the mapped flags changes."""
        super().update_state(status)
        if self.hass and any(
            attribute in status for attribute in self._flags_to_state()
        ):
            self.schedule_update_if_running()

    def _flags_to_state(self) -> dict[str, str]:
        """Flag attribute to state mapping from the entity config.

        Returns
        -------
        State strings keyed by their flag attribute names.

        """
        return cast("dict[str, str]", self._config["state_from_flags"])


class MideaEstimatedUsageSensor(MideaSensor, RestoreEntity):
    """Represent estimated dishwasher usage accumulated per run.

    The dishwasher does not report actual energy/water usage, so this sensor
    accumulates a fixed per-mode estimate (from the product manual) once each
    time a wash run *completes*. Completion is detected by the device
    ``progress`` attribute reaching ``"complete"``; a cancelled or errored run
    never reaches that state and is therefore not counted. The estimate table
    is keyed by mode code, so it doesn't depend on the library's mode names,
    and chosen per model (``values_by_model``), since figures differ by model.
    """

    # Values reported by midea-lan since 2026.9.2 (lowercase keys).
    _COMPLETE_PROGRESS = "complete"
    _RUNNING_STATUS = "running"

    def __init__(self, device: MideaDevice, entity_key: str) -> None:
        """Initialize estimated usage sensor."""
        super().__init__(device, entity_key)
        self._native_value: float = 0.0
        self._last_progress: str | None = None
        self._running_mode: str | None = None

    async def async_added_to_hass(self) -> None:
        """Register for device updates and restore the accumulated value."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            try:
                self._native_value = float(last_state.state)
            except (TypeError, ValueError):
                self._native_value = 0.0
        self._last_progress = cast(
            "str | None",
            self._device.get_attribute("progress"),
        )
        if self._device.get_attribute("status") == self._RUNNING_STATUS:
            self._running_mode = cast("str | None", self._device.get_attribute("mode"))

    @property
    def native_value(self) -> StateType:
        """Accumulated estimated usage."""
        return round(self._native_value, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Estimate metadata for the sensor."""
        modes = self._modes()
        return {
            "estimate_source": "fixed_per_wash_mode",
            "known_modes": [modes.get(code, str(code)) for code in self._values()],
            "last_progress": self._last_progress,
            "running_mode": self._running_mode,
        }

    def _values(self) -> dict[int, float]:
        """Return this model's per-run estimate, keyed by mode code.

        Returns
        -------
        Estimate per mode code, empty if the model has no figures.

        """
        by_model = cast(
            "dict[str, dict[int, float]]",
            self._config["estimate"]["values_by_model"],
        )
        return by_model.get(str(self._device.model), {})

    def _modes(self) -> dict[int, str]:
        """Return the dishwasher's mode names keyed by code.

        Returns
        -------
        Mode names keyed by mode code.

        """
        if isinstance(self._device, MideaE1Device):
            return self._device.modes
        return {}

    def _mode_code(self, mode: str | None) -> int | None:
        """Return the code of a reported mode name.

        Returns
        -------
        The mode code, or None if the name is unknown.

        """
        modes = self._modes()
        return next((code for code, name in modes.items() if name == mode), None)

    def update_state(self, status: Any) -> None:  # ruff:ignore[any-type]
        """Accumulate the estimate once when a dishwasher run completes."""
        current_status = self._device.get_attribute("status")
        current_progress = cast(
            "str | None",
            self._device.get_attribute("progress"),
        )
        current_mode = cast("str | None", self._device.get_attribute("mode"))

        # Remember the mode selected while the machine is actually running so we
        # can still attribute usage after it stops reporting the mode on finish.
        if current_status == self._RUNNING_STATUS:
            self._running_mode = current_mode

        # Count once, on the edge into the "Complete" progress state. A cancel or
        # error transition never reaches "Complete", so it is not counted.
        if (
            current_progress == self._COMPLETE_PROGRESS
            and self._last_progress != self._COMPLETE_PROGRESS
        ):
            mode = self._running_mode or current_mode
            values = self._values()
            code = self._mode_code(mode)
            if code in values:
                self._native_value += values[code]
            self._running_mode = None

        self._last_progress = current_progress
        super().update_state(status)
        if self.hass and (
            "progress" in status or "status" in status or "mode" in status
        ):
            self.schedule_update_if_running()
