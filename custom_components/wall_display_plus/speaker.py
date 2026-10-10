"""Clip cache and playback for one wall display.

A clip is cached under a key derived from what was asked to play (for text to
speech: engine, message, language and options). The key is an HMAC with a
random secret per display, kept in the cache store: without it nobody can tell
from a file name on the display which text it holds, even by trying likely
texts. Clearing the cache creates a new secret. The cache remembers the
clip's file name on the display and when it was last played. Before playing,
the display's media library is checked by file name; if the file is gone, the
clip is uploaded again as if it had never been cached.

Before every upload the speaker makes room: the clips of this integration may
take at most cache_max_bytes, and the display must keep FREE_SPACE_RESERVE
free. Orphaned clips go first, then the ones played longest ago. A single
clip larger than the whole cache is played once and not kept.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import MediaItem, WallDisplayClient, WallDisplayError, WallDisplayStorageError
from .const import (
    CLIP_PREFIX,
    DOMAIN,
    FREE_SPACE_RESERVE,
    MEDIA_TYPE_ALERT,
    PLAYBACK_MARGIN,
    STORAGE_VERSION,
)

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


def cache_key(secret: bytes, source: str) -> str:
    """Return the cache key for what was asked to play."""
    return hmac.new(secret, source.encode(), hashlib.sha256).hexdigest()


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
        cache_max_bytes: int,
    ) -> None:
        """Initialize the speaker."""
        self._hass = hass
        self._client = client
        self.cache_days = cache_days
        self.cache_max_bytes = cache_max_bytes
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.cache"
        )
        # key -> {"filename": str, "last_used": iso timestamp}
        self._entries: dict[str, dict[str, str]] = {}
        self._secret = secrets.token_bytes(32)
        self._lock = asyncio.Lock()
        self._busy_until = 0.0
        self.playing_until = 0.0

    @property
    def entries(self) -> dict[str, dict[str, str]]:
        """Return the cache entries (for diagnostics)."""
        return self._entries

    def key_for(self, source: str) -> str:
        """Return the cache key for what was asked to play."""
        return cache_key(self._secret, source)

    async def async_load(self) -> None:
        """Load the cache and its secret from storage."""
        data = await self._store.async_load() or {}
        if secret := data.get("secret"):
            self._secret = bytes.fromhex(secret)
            self._entries = dict(data.get("entries", {}))
        else:
            # A store without a secret (or none at all): start with a new
            # secret. Entries keyed the old way are dropped, so the next
            # cleanup removes their files from the display.
            self._entries = {}
            self._save()

    def _data(self) -> dict[str, Any]:
        return {"secret": self._secret.hex(), "entries": self._entries}

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 5)

    async def async_flush(self) -> None:
        """Write the cache now (before the entry unloads)."""
        await self._store.async_save(self._data())

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
        if audio.key is not None:
            filename = clip_filename(audio.key, audio.extension)
            if (entry := self._entries.get(audio.key)) and entry["filename"] == filename:
                item = await self._client.find_media(filename, attempts=1)
                if item is not None:
                    return item
                _LOGGER.debug("Cached clip %s is gone from the display", filename)
        await self._make_room(audio)
        if audio.key is None:
            filename = clip_filename(secrets.token_hex(12), audio.extension)
        else:
            filename = clip_filename(audio.key, audio.extension)
        await self._client.upload(filename, MEDIA_TYPE_ALERT, audio.data)
        item = await self._client.find_media(filename)
        if item is None:
            raise WallDisplayError(f"{filename} is not in the media library after the upload")
        return item

    async def _make_room(self, audio: ClipAudio) -> None:
        """Keep the cache under its limit and enough storage free for the clip.

        A cached clip larger than the whole cache is turned into an uncached
        one (audio.key is cleared). Raises WallDisplayStorageError when even
        after deleting every clip of this integration the display would have
        less than FREE_SPACE_RESERVE left.
        """
        needed = len(audio.data)
        if audio.key is not None and needed > self.cache_max_bytes:
            _LOGGER.debug("Clip of %s bytes exceeds the cache limit, not caching it", needed)
            audio.key = None

        ours = [i for i in await self._client.list_media() if i.filename.startswith(CLIP_PREFIX)]
        last_used = {
            e["filename"]: parse_timestamp(e.get("last_used")) for e in self._entries.values()
        }
        oldest = datetime.min.replace(tzinfo=dt_util.UTC)
        # Orphans (not in the cache) first, then the clip played longest ago
        candidates = sorted(
            ours, key=lambda i: (i.filename in last_used, last_used.get(i.filename, oldest))
        )

        if audio.key is not None:
            used = sum(i.size for i in ours)
            while candidates and used + needed > self.cache_max_bytes:
                victim = candidates.pop(0)
                if await self._evict(victim):
                    used -= victim.size

        free = (await self._client.get_device_info()).fs_free
        if free is None:
            return  # the firmware does not report it; nothing to check
        while candidates and free - needed < FREE_SPACE_RESERVE:
            victim = candidates.pop(0)
            if await self._evict(victim):
                free += victim.size
        if free - needed < FREE_SPACE_RESERVE:
            raise WallDisplayStorageError(free, needed)

    async def _evict(self, item: MediaItem) -> bool:
        """Delete one of our clips and forget it."""
        if not await self._try_delete(item):
            return False
        for key in [k for k, e in self._entries.items() if e["filename"] == item.filename]:
            del self._entries[key]
        self._save()
        _LOGGER.debug("Evicted %s (%s bytes) to make room", item.filename, item.size)
        return True

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
                if parse_timestamp(entry.get("last_used")) < limit
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
        """Delete every clip of this integration, forget the cache, new secret.

        The new secret makes hashes from diagnostics shared before useless.
        """
        async with self._lock:
            deleted = 0
            for item in await self._client.list_media():
                if item.filename.startswith(CLIP_PREFIX):
                    deleted += await self._try_delete(item)
            self._entries = {}
            self._secret = secrets.token_bytes(32)
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


def parse_timestamp(value: str | None) -> datetime:
    """Parse a stored timestamp; one without time zone counts as UTC."""
    parsed = dt_util.parse_datetime(value) if value else None
    if parsed is None:
        return datetime.min.replace(tzinfo=dt_util.UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)
