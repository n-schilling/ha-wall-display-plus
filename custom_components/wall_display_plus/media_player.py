"""Speaker media players: one per display, and speaker groups (experimental)."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components import media_source
from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.components.media_player.browse_media import BrowseMedia
from homeassistant.components.media_player.const import (
    ATTR_MEDIA_EXTRA,
    DOMAIN as MEDIA_PLAYER_DOMAIN,
    MediaPlayerDeviceClass,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_MEDIA_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .api import (
    WallDisplayAuthError,
    WallDisplayError,
    WallDisplayStorageError,
    WallDisplayUnsupportedError,
)
from .audio import tts_message
from .const import (
    CONF_MAX_TEXT_LENGTH,
    CONF_MEMBERS,
    DEFAULT_MAX_TEXT_LENGTH,
    DOMAIN,
    FREE_SPACE_RESERVE,
    MEDIA_TYPE_ALERT,
)
from .coordinator import WallDisplayConfigEntry, is_group
from .entity import WallDisplayEntity
from .speaker import Speaker
from .sync import SYNC_TIMEOUT, SyncTimeoutError, get_sync_manager

_LOGGER = logging.getLogger(__name__)

# Announcements are serialized by the speaker itself.
PARALLEL_UPDATES = 0

SUPPORTED_FEATURES = (
    MediaPlayerEntityFeature.PLAY_MEDIA
    | MediaPlayerEntityFeature.BROWSE_MEDIA
    | MediaPlayerEntityFeature.MEDIA_ANNOUNCE
    | MediaPlayerEntityFeature.STOP
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the speaker of a display, or a speaker group."""
    if is_group(entry):
        async_add_entities([SpeakerGroup(entry)])
    else:
        async_add_entities([WallDisplaySpeaker(entry)])


def unsupported_issue_id(entry: WallDisplayConfigEntry) -> str:
    """Return the id of the repair issue for an incompatible firmware."""
    return f"unsupported_firmware_{entry.entry_id}"


def storage_issue_id(entry: WallDisplayConfigEntry) -> str:
    """Return the id of the repair issue for a display that is almost full."""
    return f"storage_full_{entry.entry_id}"


async def _async_browse(hass: HomeAssistant, media_content_id: str | None) -> BrowseMedia:
    """Browse audio from media sources."""
    return await media_source.async_browse_media(
        hass,
        media_content_id,
        content_filter=lambda item: item.media_content_type.startswith("audio/"),
    )


class WallDisplaySpeaker(WallDisplayEntity, MediaPlayerEntity):
    """Plays announcements and audio clips on the display's speaker.

    Audio is uploaded to the display's media library as an alert clip and
    played once. Clips are cached on the display; see speaker.py. Calls of
    one action for several speakers play together; see sync.py.
    """

    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the speaker."""
        super().__init__(entry, "speaker")
        self._end_listener: Any = None

    @property
    def speaker(self) -> Speaker:
        """Return the speaker of the display."""
        return self._entry.runtime_data.speaker

    @property
    def state(self) -> MediaPlayerState:
        """Return playing while an alert clip plays."""
        if time.monotonic() < self.speaker.playing_until:
            return MediaPlayerState.PLAYING
        data = self.coordinator.data
        playback = data.media_status.get("playback") or {}
        if (
            playback.get("enable")
            and playback.get("media_type") == MEDIA_TYPE_ALERT
            # A status read before our last clip ended may still show that clip
            and data.fetched_at > self.speaker.playing_until
        ):
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    async def async_added_to_hass(self) -> None:
        """Make the speaker known to speaker groups."""
        await super().async_added_to_hass()
        members = get_sync_manager(self.hass).members
        members[self.entity_id] = self

        @callback
        def _forget() -> None:
            members.pop(self.entity_id, None)

        self.async_on_remove(_forget)

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play text to speech, a media source item or a URL.

        extra: {cache: false} plays without keeping the clip, {volume: 0.0-1.0}
        plays this clip at its own volume and goes back afterwards.
        """
        extra = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        volume = _extra_volume(extra.get("volume"))
        await get_sync_manager(self.hass).async_play(self._context, media_id, extra, volume, [self])

    def check_request(self, media_id: str) -> None:
        """Refuse a text longer than allowed, before the engine runs."""
        if (message := tts_message(media_id)) is None:
            return
        limit = int(self._entry.options.get(CONF_MAX_TEXT_LENGTH, DEFAULT_MAX_TEXT_LENGTH))
        if len(message) > limit:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="text_too_long",
                translation_placeholders={"length": str(len(message)), "max": str(limit)},
            )

    @callback
    def played(self, duration: float, delay: float) -> None:
        """Record a clip that started."""
        self.speaker.last_delay = delay
        self.coordinator.async_update_listeners()
        ir.async_delete_issue(self.hass, DOMAIN, unsupported_issue_id(self._entry))
        ir.async_delete_issue(self.hass, DOMAIN, storage_issue_id(self._entry))
        self.async_write_ha_state()
        if self._end_listener:
            self._end_listener()
        self._end_listener = async_call_later(self.hass, duration + 1, self._clip_ended)

    def play_error(self, err: Exception) -> Exception:
        """Turn an error of the display into the one the action call raises."""
        if isinstance(err, SyncTimeoutError):
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="sync_timeout",
                translation_placeholders={"timeout": str(round(SYNC_TIMEOUT))},
            )
        if isinstance(err, WallDisplayAuthError):
            self._entry.async_start_reauth(self.hass)
            return HomeAssistantError(translation_domain=DOMAIN, translation_key="invalid_auth")
        if isinstance(err, WallDisplayStorageError):
            self._create_storage_issue(err)
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="storage_full",
                translation_placeholders={"free_mb": _mb(err.free)},
            )
        if isinstance(err, WallDisplayUnsupportedError):
            self._create_unsupported_issue(str(err))
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unsupported_firmware",
                translation_placeholders={"error": str(err)},
            )
        if isinstance(err, WallDisplayError):
            # Perhaps the display is gone: refresh now, so the entities show
            # it instead of waiting for the next poll.
            self.hass.async_create_task(self.coordinator.async_request_refresh())
            return HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="play_failed",
                translation_placeholders={"error": str(err)},
            )
        return err

    def _create_unsupported_issue(self, error: str) -> None:
        device = self._entry.runtime_data.device
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            unsupported_issue_id(self._entry),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="unsupported_firmware",
            translation_placeholders={
                "title": self._entry.title,
                "model": device.model,
                "firmware": device.firmware,
                "error": error,
            },
            learn_more_url="https://github.com/n-schilling/ha-wall-display-plus/issues",
        )

    def _create_storage_issue(self, err: WallDisplayStorageError) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            storage_issue_id(self._entry),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="storage_full",
            translation_placeholders={
                "title": self._entry.title,
                "free_mb": _mb(err.free),
                "reserve_mb": _mb(FREE_SPACE_RESERVE),
            },
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending end-of-clip callback."""
        if self._end_listener:
            self._end_listener()
            self._end_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _clip_ended(self, _now: Any) -> None:
        # Expired and orphaned clips are deleted by the daily cleanup
        self._end_listener = None
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        """Stop the clip that is playing."""
        try:
            await self._entry.runtime_data.client.stop()
        except WallDisplayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="stop_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.speaker.stopped()
        self.async_write_ha_state()

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse audio from media sources."""
        return await _async_browse(self.hass, media_content_id)


class SpeakerGroup(MediaPlayerEntity):
    """Plays on all its member speakers at the same time (experimental).

    The members play together like the speakers of one tts.speak call; see
    sync.py. A member that is unavailable is left out.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = SUPPORTED_FEATURES

    def __init__(self, entry: ConfigEntry[Any]) -> None:
        """Initialize the group."""
        # Registry IDs: the members stay members when their entity ID changes
        self._member_ids: list[str] = list(entry.options.get(CONF_MEMBERS, []))
        self._members: list[str] = []
        self._unsubscribe_states: CALLBACK_TYPE | None = None
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            model="Speaker group",
            entry_type=DeviceEntryType.SERVICE,
        )

    def _available_members(self) -> list[str]:
        return [
            entity_id
            for entity_id in self._members
            if (state := self.hass.states.get(entity_id)) is not None
            and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        ]

    @property
    def available(self) -> bool:
        """Return whether at least one member is available."""
        return bool(self._available_members())

    @property
    def state(self) -> MediaPlayerState:
        """Return playing while any member plays."""
        for entity_id in self._members:
            if (
                state := self.hass.states.get(entity_id)
            ) and state.state == MediaPlayerState.PLAYING:
                return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the members' entity IDs."""
        return {ATTR_ENTITY_ID: self._members}

    async def async_added_to_hass(self) -> None:
        """Follow the members' states and entity IDs."""
        self._resolve_members()

        @callback
        def _registry_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            data = event.data
            if data["entity_id"] in self._members or (
                data["action"] == "update" and data.get("old_entity_id") in self._members
            ):
                self._resolve_members()
                self.async_write_ha_state()

        self.async_on_remove(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _registry_changed)
        )
        self.async_on_remove(self._stop_following_states)

    @callback
    def _resolve_members(self) -> None:
        registry = er.async_get(self.hass)
        resolved = (er.async_resolve_entity_id(registry, m) for m in self._member_ids)
        self._members = [entity_id for entity_id in resolved if entity_id]
        self._stop_following_states()

        @callback
        def _member_changed(_event: Event[EventStateChangedData]) -> None:
            self.async_write_ha_state()

        self._unsubscribe_states = async_track_state_change_event(
            self.hass, self._members, _member_changed
        )

    @callback
    def _stop_following_states(self) -> None:
        if self._unsubscribe_states:
            self._unsubscribe_states()
            self._unsubscribe_states = None

    async def async_play_media(
        self, media_type: MediaType | str, media_id: str, **kwargs: Any
    ) -> None:
        """Play on all available members together (same extra as a speaker)."""
        extra = kwargs.get(ATTR_MEDIA_EXTRA) or {}
        volume = _extra_volume(extra.get("volume"))
        manager = get_sync_manager(self.hass)
        members = [
            manager.members[entity_id]
            for entity_id in self._available_members()
            if entity_id in manager.members
        ]
        if not members:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="group_unavailable")
        await manager.async_play(self._context, media_id, extra, volume, members)

    async def async_media_stop(self) -> None:
        """Stop all members."""
        if members := self._available_members():
            await self.hass.services.async_call(
                MEDIA_PLAYER_DOMAIN,
                SERVICE_MEDIA_STOP,
                {ATTR_ENTITY_ID: members},
                blocking=True,
                context=self._context,
            )

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse audio from media sources."""
        return await _async_browse(self.hass, media_content_id)


def _mb(size: int) -> str:
    """Format bytes as megabytes for messages."""
    return str(round(size / 1024 / 1024))


def _extra_volume(value: Any) -> int | None:
    """Turn extra.volume (0.0-1.0, like volume_set) into the display's 0-10."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_volume",
            translation_placeholders={"volume": str(value)},
        )
    return round(value * 10)
