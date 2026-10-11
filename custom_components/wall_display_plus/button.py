"""Buttons: test the announcement volume, clear the speaker cache, restart the display app."""

from __future__ import annotations

from typing import Any

from homeassistant.components import tts
from homeassistant.components.button import ButtonEntity
from homeassistant.components.button.const import ButtonDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import WallDisplayError
from .const import DOMAIN
from .coordinator import WallDisplayConfigEntry
from .entity import WallDisplayEntity
from .sync import get_sync_manager

PARALLEL_UPDATES = 1

# Spoken by "Test announcement volume", in Home Assistant's language
TEST_MESSAGES = {
    "de": "Das ist ein Test der Ansage-Lautstärke.",
    "en": "This is a test of the announcement volume.",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    async_add_entities([TestVolumeButton(entry), ClearCacheButton(entry), RestartAppButton(entry)])


def _voice(hass: HomeAssistant) -> tuple[str | None, str, dict[str, Any] | None]:
    """Return engine, language and options of the voice Home Assistant speaks with.

    That is the text-to-speech of the preferred voice assistant, otherwise
    the default engine in Home Assistant's language.
    """
    if (voice := _assistant_voice(hass)) is not None:
        return voice
    return tts.async_default_engine(hass), hass.config.language, None


def _assistant_voice(hass: HomeAssistant) -> tuple[str, str, dict[str, Any] | None] | None:
    """Return the voice of the preferred voice assistant, if one speaks."""
    if "assist_pipeline" not in hass.config.components:
        return None
    # Imported only when loaded: the module pulls in the assistant's requirements
    from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

    pipeline = async_get_pipeline(hass)
    if not pipeline.tts_engine:
        return None
    options = {"voice": pipeline.tts_voice} if pipeline.tts_voice else None
    return pipeline.tts_engine, pipeline.tts_language or hass.config.language, options


class TestVolumeButton(WallDisplayEntity, ButtonEntity):
    """Speak a test sentence at the announcement volume."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(entry, "test_volume")

    async def async_press(self) -> None:
        """Speak the test sentence on this display's speaker."""
        engine, language, options = _voice(self.hass)
        if engine is None:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="no_tts_engine")
        message = TEST_MESSAGES.get(language.split("-")[0].lower(), TEST_MESSAGES["en"])
        media_id = tts.generate_media_source_id(
            self.hass, message, engine=engine, language=language, options=options
        )
        manager = get_sync_manager(self.hass)
        speaker_id = er.async_get(self.hass).async_get_entity_id(
            "media_player", DOMAIN, f"{self._entry.runtime_data.device.mac}_speaker"
        )
        if speaker_id is None or (speaker := manager.members.get(speaker_id)) is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="speaker_unavailable"
            )
        await manager.async_play(self._context, media_id, {}, None, [speaker])


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
