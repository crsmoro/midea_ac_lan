"""Local last-cycle extension for the one verified DB washer profile.

The published midea-lan 2026.10.0 remains responsible for all normal device
behavior. Only 0405 estimates are added here; their scaling is based on one
correlated appliance report and app observation.
"""

from typing import Any

from midealan.const import DeviceType, ProtocolVersion
from midealan.device import MideaDevice
from midealan.devices import device_selector as sdk_device_selector
from midealan.devices.db import MideaDBDevice
from midealan.message import MessageResponse, MessageType

WATER = "last_cycle_water_consumption"
ENERGY = "last_cycle_energy_consumption"
CYCLE_REPORT_SEQUENCE = "cycle_report_sequence"
MODEL = "38132406"
SUBTYPE = 19504
FRAME_START = 0xAA
TOSHIBA_FRAME_MAGIC = bytes.fromhex("55aacc33")
CONSUMPTION_BODY_TYPE = 0x05


class CycleEstimateDBDevice(MideaDBDevice):
    """Add two optional attributes without changing SDK classes globally."""

    def __init__(self, **kwargs: Any) -> None:  # ruff: ignore[any-type]
        """Initialize the SDK device and unknown last-cycle estimates."""
        super().__init__(**kwargs)
        self._attributes.update(
            {WATER: None, ENERGY: None, CYCLE_REPORT_SEQUENCE: 0},
        )

    def process_message(self, msg: bytes) -> dict[str, Any]:
        """Extend SDK processing for complete 0405 notification bodies.

        Returns
        -------
        Attribute updates to publish to SDK listeners.

        """
        header = MessageResponse.HEADER_LENGTH
        if msg.startswith(TOSHIBA_FRAME_MAGIC):
            return super().process_message(msg)
        if len(msg) < header + 2 or msg[0] != FRAME_START or msg[2] != DeviceType.DB:
            return {}
        if getattr(self, "toshiba_protocol", False):
            return super().process_message(msg)
        try:
            message_type = MessageType(msg[header - 1])
        except ValueError:
            return {}
        if message_type == MessageType.notify1 and msg[header] == CONSUMPTION_BODY_TYPE:
            # LocalSecurity.aes_decrypt removes AES padding before dispatch.
            # A decrypted report must contain exactly its declared frame.
            if msg[1] < header + 5 or len(msg) != msg[1] + 1 or sum(msg[1:]) & 0xFF:
                return {}
            status = super().process_message(msg)
            if self.model == MODEL and self.subtype == SUBTYPE:
                values = {
                    WATER: int.from_bytes(msg[header + 1 : header + 3], "little") / 10,
                    ENERGY: int.from_bytes(msg[header + 3 : header + 5], "little")
                    / 1000,
                }
                self._attributes.update(values)
                self._attributes[CYCLE_REPORT_SEQUENCE] += 1
                status.update(values)
                status[CYCLE_REPORT_SEQUENCE] = self._attributes[CYCLE_REPORT_SEQUENCE]
            return status
        return super().process_message(msg)


def device_selector(  # ruff: ignore[too-many-arguments,too-many-positional-arguments]
    name: str,
    device_id: int,
    device_type: int,
    ip_address: str,
    port: int,
    token: str,
    key: str,
    device_protocol: ProtocolVersion,
    model: str,
    subtype: int,
    customize: str,
    mac: str | None = None,
    serial_number: str | None = None,
    sn8: str | None = None,
    manufacturer_code: str | None = None,
    cloud_name: str | None = None,
) -> MideaDevice:
    """Select the adapter only for the verified type/model/subtype triple.

    Returns
    -------
    The selected SDK device, with a local adapter for the verified profile.

    """
    kwargs: dict[str, Any] = {
        "name": name,
        "device_id": device_id,
        "ip_address": ip_address,
        "port": port,
        "token": token,
        "key": key,
        "device_protocol": device_protocol,
        "model": model,
        "subtype": subtype,
        "customize": customize,
        "mac": mac,
        "serial_number": serial_number,
        "sn8": sn8,
        "manufacturer_code": manufacturer_code,
        "cloud_name": cloud_name,
    }
    if device_type == DeviceType.DB and model == MODEL and subtype == SUBTYPE:
        return CycleEstimateDBDevice(**kwargs)
    return sdk_device_selector(device_type=device_type, **kwargs)
