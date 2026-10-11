"""Diagnostic sensors of the wall display and of the speaker."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import CLIP_PREFIX
from .coordinator import DisplayState, WallDisplayConfigEntry
from .entity import WallDisplayEntity
from .speaker import Speaker

PARALLEL_UPDATES = 0

# The app start time is computed from the uptime; it moves by a few seconds
# with every poll. A new value counts only if it differs by more than this.
APP_START_TOLERANCE = timedelta(minutes=1)


@dataclass(frozen=True, kw_only=True)
class WallDisplaySensorDescription(SensorEntityDescription):
    """A sensor and where its value comes from."""

    value: Callable[[DisplayState, Speaker], float | int | None]
    attributes: Callable[[Speaker], dict[str, Any]] | None = None


SENSORS: tuple[WallDisplaySensorDescription, ...] = (
    WallDisplaySensorDescription(
        key="memory_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state, _: state.device.ram_free if state.device else None,
    ),
    WallDisplaySensorDescription(
        key="storage_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state, _: state.device.fs_free if state.device else None,
    ),
    WallDisplaySensorDescription(
        key="cpu_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value=lambda state, _: state.cpu_temperature,
    ),
    WallDisplaySensorDescription(
        key="cache_clips",
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state, _: state.cache_clips,
        # Every clip of this entry starts with it; shows which files in the
        # display's media library belong here. The texts are never shown.
        attributes=lambda speaker: {"file_prefix": f"{CLIP_PREFIX}{speaker.tag}_"},
    ),
    WallDisplaySensorDescription(
        key="cache_size",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state, _: state.cache_bytes,
    ),
    WallDisplaySensorDescription(
        key="announcement_delay",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda _, speaker: speaker.last_delay,
    ),
    WallDisplaySensorDescription(
        key="cache_hit_rate",
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value=lambda _, speaker: speaker.cache_hit_rate,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    async_add_entities(
        [WallDisplaySensor(entry, description) for description in SENSORS]
        + [AppStartedSensor(entry)]
    )


class WallDisplaySensor(WallDisplayEntity, SensorEntity):
    """A diagnostic value of the display or the speaker."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"file_prefix"})
    entity_description: WallDisplaySensorDescription

    def __init__(
        self, entry: WallDisplayConfigEntry, description: WallDisplaySensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        """Return the value."""
        return self.entity_description.value(
            self.coordinator.data, self._entry.runtime_data.speaker
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if the sensor has any."""
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(self._entry.runtime_data.speaker)


class AppStartedSensor(WallDisplayEntity, SensorEntity):
    """When the display app last started.

    The official integration shows when the device started. A later app
    start without a device restart means the app crashed or was restarted.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry, "app_started")
        self._started: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        """Return the app start, stable against the jitter of the uptime."""
        device = self.coordinator.data.device
        if device is None or device.app_uptime is None:
            return None
        started = dt_util.utcnow() - timedelta(seconds=device.app_uptime)
        if self._started is None or abs(started - self._started) > APP_START_TOLERANCE:
            self._started = started.replace(microsecond=0)
        return self._started
