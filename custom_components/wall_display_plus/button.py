"""Buttons: clear the speaker cache, restart the display app."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import WallDisplayError
from .const import DOMAIN
from .coordinator import WallDisplayConfigEntry
from .entity import WallDisplayEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities([ClearCacheButton(entry), RestartAppButton(entry)])


class ClearCacheButton(WallDisplayEntity, ButtonEntity):
    """Delete every clip this integration put on the display.

    Useful after changing the voice of the text-to-speech engine: cached
    announcements would otherwise keep the old voice until they expire.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "clear_cache")

    async def async_press(self) -> None:
        """Delete the clips and forget the cache."""
        try:
            await self._entry.runtime_data.speaker.async_clear()
        except WallDisplayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="clear_failed",
                translation_placeholders={"error": str(err)},
            ) from err


class RestartAppButton(WallDisplayEntity, ButtonEntity):
    """Restart only the display app, not the whole device.

    Faster than a device restart and enough when the dashboard hangs. The
    official integration's restart button restarts the whole device.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "restart_app")

    async def async_press(self) -> None:
        """Restart the display app."""
        try:
            await self._entry.runtime_data.client.restart_app()
        except WallDisplayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="restart_failed",
                translation_placeholders={"error": str(err)},
            ) from err
