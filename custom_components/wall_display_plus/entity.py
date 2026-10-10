"""Base entity for Wall Display Plus."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WallDisplayConfigEntry, WallDisplayCoordinator


class WallDisplayEntity(CoordinatorEntity[WallDisplayCoordinator]):
    """An entity of the wall display.

    Since Home Assistant 2026.10 a device belongs to a single integration, so
    these entities get their own device. It carries the display's MAC address
    as connection, like the device of the display's official integration.
    """

    _attr_has_entity_name = True

    def __init__(self, entry: WallDisplayConfigEntry, key: str) -> None:
        """Initialize the entity."""
        super().__init__(entry.runtime_data.coordinator)
        device = entry.runtime_data.device
        self._entry = entry
        self._attr_unique_id = f"{device.mac}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, format_mac(device.mac))},
            identifiers={(DOMAIN, device.mac)},
            name=entry.title,
            model=device.model,
            sw_version=device.firmware,
        )
