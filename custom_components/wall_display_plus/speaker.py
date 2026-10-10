"""Clip cache and playback for one wall display.

A clip is cached under a key derived from what was asked to play (for text to
speech: engine, message, language and options). The cache remembers the
clip's file name on the display and when it was last played. Before playing,
the display's media library is checked by file name; if the file is gone, the
clip is uploaded again as if it had never been cached.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import MediaItem, WallDisplayClient, WallDisplayError
from .const import CLIP_PREFIX, DOMAIN, MEDIA_TYPE_ALERT, PLAYBACK_MARGIN, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

KNOWN_EXTENSIONS = ("mp3", "wav", "ogg", "flac", "aac", "m4a")

# An uncached clip is deleted this long after it finished playing.
DELETE_GRACE = 2.0


@dataclass(slots=True)
class ClipAudio:
    """Audio to play, with the key it is cached under (None: do not cache)."""

    key: str | None
    extension: str
    data: bytes


def cache_key(source: str) -> str:
    """Return the cache key for what was asked to play."""
    return hashlib.sha256(source.encode()).hexdigest()


def clip_filename(key: str, extension: str) -> str:
    """Return the file name a clip gets on the display."""
    extension = extension.lower().lstrip(".")
    if extension not in KNOWN_EXTENSIONS:
        extension = "mp3"
    return f"{CLIP_PREFIX}{key[:24]}.{extension}"


class Speaker:
    """Upload, cache, play and clean up clips on one display."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: WallDisplayClient,
        entry_id: str,
        cache_days: int,
    ) -> None:
        """Initialize the speaker."""
        self._hass = hass
        self._client = client
        self.cache_days = cache_days
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.cache"
        )
        # key -> {"filename": str, "last_used": iso timestamp}
        self._entries: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._busy_until = 0.0
        self.playing_until = 0.0

    @property
    def entries(self) -> dict[str, dict[str, str]]:
        """Return the cache entries (for diagnostics)."""
        return self._entries

    async def async_load(self) -> None:
        """Load the cache from storage."""
        data = await self._store.async_load() or {}
        self._entries = dict(data.get("entries", {}))

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"entries": self._entries}, 5)

    async def async_flush(self) -> None:
        """Write the cache now (before the entry unloads)."""
        await self._store.async_save({"entries": self._entries})

    async def async_play(self, audio: ClipAudio) -> float:
        """Play a clip; return its duration in seconds (0 when unknown).

        Clips are played one after another: a second announcement waits until
        the first one has finished.
        """
        async with self._lock:
            wait = self._busy_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)

            item = await self._ensure_on_display(audio)
            result = await self._client.play_alert(item.id)
            duration = result.duration
            now = time.monotonic()
            self._busy_until = now + duration + PLAYBACK_MARGIN
            self.playing_until = now + duration

            if audio.key is None:
                # Not cached: delete it once it has been played.
                self._hass.async_create_background_task(
                    self._delete_later(item.id, duration + PLAYBACK_MARGIN + DELETE_GRACE),
                    f"{DOMAIN} delete uncached clip",
                )
            else:
                self._entries[audio.key] = {
                    "filename": item.filename,
                    "last_used": dt_util.utcnow().isoformat(),
                }
                self._save()
            return duration

    async def _ensure_on_display(self, audio: ClipAudio) -> MediaItem:
        if audio.key is None:
            filename = clip_filename(cache_key(f"uncached:{time.time_ns()}"), audio.extension)
        else:
            filename = clip_filename(audio.key, audio.extension)
            if (entry := self._entries.get(audio.key)) and entry["filename"] == filename:
                item = await self._client.find_media(filename, attempts=1)
                if item is not None:
                    return item
                _LOGGER.debug("Cached clip %s is gone from the display", filename)
        await self._client.upload(filename, MEDIA_TYPE_ALERT, audio.data)
        item = await self._client.find_media(filename)
        if item is None:
            raise WallDisplayError(f"{filename} is not in the media library after the upload")
        return item

    async def _delete_later(self, media_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        try:
            await self._client.delete_media(media_id)
        except WallDisplayError as err:
            _LOGGER.debug("Could not delete clip %s: %s", media_id, err)

    async def async_cleanup(self) -> int:
        """Delete clips not played for cache_days, and our clips nobody knows.

        Returns the number of deleted files.
        """
        async with self._lock:
            limit = dt_util.utcnow() - timedelta(days=self.cache_days)
            expired = {
                key
                for key, entry in self._entries.items()
                if _parse(entry.get("last_used")) < limit
            }
            keep = {entry["filename"] for key, entry in self._entries.items() if key not in expired}
            deleted = 0
            for item in await self._client.list_media():
                if item.filename.startswith(CLIP_PREFIX) and item.filename not in keep:
                    deleted += await self._try_delete(item)
            if expired:
                for key in expired:
                    self._entries.pop(key, None)
                self._save()
            return deleted

    async def async_clear(self) -> int:
        """Delete every clip of this integration and forget the cache."""
        async with self._lock:
            deleted = 0
            for item in await self._client.list_media():
                if item.filename.startswith(CLIP_PREFIX):
                    deleted += await self._try_delete(item)
            self._entries = {}
            self._save()
            return deleted

    async def _try_delete(self, item: MediaItem) -> int:
        """Delete one file; a file that vanished meanwhile is no error."""
        try:
            await self._client.delete_media(item.id)
        except WallDisplayError as err:
            _LOGGER.debug("Could not delete %s: %s", item.filename, err)
            return 0
        return 1


def _parse(value: str | None) -> datetime:
    parsed = dt_util.parse_datetime(value) if value else None
    return parsed or datetime.min.replace(tzinfo=dt_util.UTC)
