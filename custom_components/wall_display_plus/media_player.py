"""Speaker media player for the wall display."""

from __future__ import annotations

import json
import logging
import mimetypes
import time
from typing import Any

from aiohttp import ClientError, ClientTimeout
import yarl

from homeassistant.components import media_source, tts
from homeassistant.components.media_player import (
    ATTR_MEDIA_EXTRA,
    BrowseMedia,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.tts.media_source import parse_media_source_id
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .api import WallDisplayError
from .const import DOMAIN, MEDIA_TYPE_ALERT
from .coordinator import WallDisplayConfigEntry
from .entity import WallDisplayEntity
from .speaker import ClipAudio, cache_key

_LOGGER = logging.getLogger(__name__)

MAX_CLIP_BYTES = 20 * 1024 * 1024
FETCH_TIMEOUT = ClientTimeout(total=30)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the speaker."""
    async_add_entities([WallDisplaySpeaker(entry)])


class WallDisplaySpeaker(WallDisplayEntity, MediaPlayerEntity):
    """Plays announcements and audio clips on the display's speaker.

    Audio is uploaded to the display's media library as an alert clip and
    played once. Clips are cached on the display; see speaker.py.
    """

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
        | MediaPlayerEntityFeature.STOP
    )

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the speaker."""
        super().__init__(entry, "speaker")
        self._end_listener: Any = None

    @property
    def state(self) -> MediaPlayerState:
        """Return playing while an alert clip plays."""
        if time.monotonic() < self._entry.runtime_data.speaker.playing_until:
            return MediaPlayerState.PLAYING
        playback = (self.coordinator.data.media_status or {}).get("playback") or {}
        if playback.get("enable") and playback.get("media_type") == MEDIA_TYPE_ALERT:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play text to speech, a media source item or a URL."""
        extra = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        audio = await self._async_get_audio(media_id)
        if extra.get("cache") is False:
            audio.key = None
        speaker = self._entry.runtime_data.speaker
        try:
            duration = await speaker.async_play(audio)
        except WallDisplayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.async_write_ha_state()
        if self._end_listener:
            self._end_listener()
        self._end_listener = async_call_later(self.hass, duration + 1, self._async_clip_ended)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending end-of-clip callback."""
        if self._end_listener:
            self._end_listener()
            self._end_listener = None
        await super().async_will_remove_from_hass()

    async def _async_clip_ended(self, _now: Any) -> None:
        self._end_listener = None
        self.async_write_ha_state()
        try:
            await self._entry.runtime_data.speaker.async_cleanup()
        except WallDisplayError as err:
            _LOGGER.debug("Clip cleanup skipped: %s", err)

    async def _async_get_audio(self, media_id: str) -> ClipAudio:
        """Turn what was asked to play into audio bytes and a cache key."""
        if media_source.is_media_source_id(media_id):
            if media_id.startswith(f"{media_source.URI_SCHEME}{tts.DOMAIN}/"):
                return await self._async_get_tts_audio(media_id)
            item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            return await self._async_fetch(item.url, item.mime_type, cache_key(media_id))
        return await self._async_fetch(media_id, None, cache_key(media_id))

    async def _async_get_tts_audio(self, media_id: str) -> ClipAudio:
        parsed = parse_media_source_id(media_id)
        if "stream" in parsed:
            key = None  # a one-time stream, nothing to cache
        else:
            options = dict(parsed["options"])
            options.pop("use_file_cache", None)
            key = cache_key(
                json.dumps(
                    {"message": parsed["message"], **options},
                    sort_keys=True,
                    default=str,
                )
            )
        try:
            extension, data = await tts.async_get_media_source_audio(self.hass, media_id)
        except HomeAssistantError:
            raise
        except Exception as err:  # the TTS engine can fail in many ways
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="tts_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        return ClipAudio(key=key, extension=extension, data=data)

    async def _async_fetch(self, url: str, mime_type: str | None, key: str | None) -> ClipAudio:
        url = async_process_play_media_url(self.hass, url)
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(url, timeout=FETCH_TIMEOUT) as resp:
                resp.raise_for_status()
                if int(resp.headers.get("Content-Length") or 0) > MAX_CLIP_BYTES:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN, translation_key="clip_too_large"
                    )
                data = await resp.content.read(MAX_CLIP_BYTES + 1)
                mime_type = mime_type or resp.content_type
        except (ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="fetch_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        if len(data) > MAX_CLIP_BYTES:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="clip_too_large")
        return ClipAudio(key=key, extension=_extension(url, mime_type), data=data)

    async def async_media_stop(self) -> None:
        """Stop the clip that is playing."""
        try:
            await self._entry.runtime_data.client.stop()
        except WallDisplayError as err:
            raise HomeAssistantError(str(err)) from err
        self._entry.runtime_data.speaker.playing_until = 0
        self.async_write_ha_state()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse audio from media sources."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )


def _extension(url: str, mime_type: str | None) -> str:
    """Pick a file extension from the MIME type or the URL."""
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type.split(";")[0].strip())
        if guessed:
            return guessed.lstrip(".").replace("mpga", "mp3")
    suffix = yarl.URL(url).suffix.lstrip(".")
    return suffix or "mp3"
