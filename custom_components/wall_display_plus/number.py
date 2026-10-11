"""Numbers: the announcement volume, and screen settings (minimum brightness, timeouts).

The manual brightness and the automatic brightness switch are left out on
purpose: while automatic brightness is on, the display reports its current
brightness (0 while the screen is off) as the manual level. Turning the
automatic off could then leave the screen black.
"""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
    RestoreNumber,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import WallDisplayError
from .const import DOMAIN
from .coordinator import WallDisplayConfigEntry, ui_value
from .entity import WallDisplayEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class ScreenNumberDescription(NumberEntityDescription):
    """A number in the display's screen configuration (Ui.GetConfig)."""

    path: tuple[str, ...]


def _timeout(key: str, path: tuple[str, ...]) -> ScreenNumberDescription:
    # The display's web interface allows 10-3600 seconds for the screen saver;
    # the same range is used for dimming and turning off.
    return ScreenNumberDescription(
        key=key,
        path=path,
        native_min_value=10,
        native_max_value=3600,
        native_step=1,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        device_class=NumberDeviceClass.DURATION,
        mode=NumberMode.BOX,
    )


def _percent(key: str, path: tuple[str, ...]) -> ScreenNumberDescription:
    return ScreenNumberDescription(
        key=key,
        path=path,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENTAGE,
        mode=NumberMode.SLIDER,
    )


NUMBERS: tuple[ScreenNumberDescription, ...] = (
    _percent("minimum_brightness", ("brightness", "min_level")),
    _timeout("dim_after", ("brightness", "auto_dim", "timeout")),
    _timeout("screen_off_after", ("brightness", "auto_off", "timeout")),
    _timeout("screen_saver_after", ("screen_saver", "timeout")),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the screen numbers."""
    async_add_entities(
        [
            AnnouncementVolumeNumber(entry),
            *(ScreenNumber(entry, description) for description in NUMBERS),
        ]
    )


class ScreenNumber(WallDisplayEntity, NumberEntity):
    """A screen setting with a numeric value."""

    _attr_entity_category = EntityCategory.CONFIG
    entity_description: ScreenNumberDescription

    def __init__(self, entry: WallDisplayConfigEntry, description: ScreenNumberDescription) -> None:
        """Initialize the number."""
        super().__init__(entry, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        value = ui_value(self.coordinator.data, self.entity_description.path)
        return float(value) if isinstance(value, int | float) else None

    async def async_set_native_value(self, value: float) -> None:
        """Write the value to the display (only if it changes)."""
        try:
            await self.coordinator.async_set_ui(self.entity_description.path, round(value))
        except WallDisplayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class AnnouncementVolumeNumber(WallDisplayEntity, RestoreNumber):
    """Volume of announcements that do not bring their own (extra.volume).

    Kept by Home Assistant, not on the display: the display has one volume
    for everything, which the speaker sets for each announcement and resets
    afterwards, so music keeps its own volume.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 10
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the number."""
        super().__init__(entry, "announcement_volume")

    async def async_added_to_hass(self) -> None:
        """Take the volume set before Home Assistant restarted."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value is not None:
            self._speaker_volume(last.native_value)

    @property
    def native_value(self) -> float:
        """Return the volume in percent."""
        return float(self._entry.runtime_data.speaker.announcement_volume * 10)

    async def async_set_native_value(self, value: float) -> None:
        """Set the volume; it applies from the next announcement on."""
        self._speaker_volume(value)
        self.async_write_ha_state()

    def _speaker_volume(self, percent: float) -> None:
        self._entry.runtime_data.speaker.announcement_volume = min(max(round(percent / 10), 0), 10)
