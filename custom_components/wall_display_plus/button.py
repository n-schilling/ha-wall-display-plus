"""Button: delete all cached clips."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up the button."""
    async_add_entities([ClearCacheButton(entry)])


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
