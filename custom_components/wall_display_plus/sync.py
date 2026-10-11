"""Play one announcement on several displays at the same time (experimental).

Home Assistant calls play_media once per target entity, all with the context
of the same action call (tts.speak with several speakers, a media player
group, a speaker group of this integration). The calls of one action that ask
for the same media with the same extra form a session:

1. The audio is fetched once (text to speech runs once).
2. Every display waits until it is free, uploads the clip if it does not
   have it, and sets the volume. It is then ready.
3. As soon as all displays are ready, all start together. The first ready
   display waits at most SYNC_TIMEOUT for the others; a display that is not
   ready by then does not play and its call fails.

Sessions with several displays that share a display run one after another,
so two sessions never wait for each other's displays; sessions on different
displays run at the same time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import copy
from dataclasses import dataclass, field
import json
import time
from typing import Any, Protocol

from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.hass_dict import HassKey

from .audio import Announcement, async_get_announcement
from .const import DOMAIN
from .speaker import ClipAudio, Speaker

# Seconds the first ready display waits for the others.
SYNC_TIMEOUT = 10.0

# Seconds a new session waits for further calls of the same action. The calls
# of one action start together, so this only needs to cover a few iterations
# of the event loop. It runs while the audio is fetched and costs no time.
JOIN_WINDOW = 0.05

DATA_SYNC: HassKey[SyncManager] = HassKey(f"{DOMAIN}_sync")


class SyncTimeoutError(Exception):
    """The display was not ready when the others started."""


class Member(Protocol):
    """A speaker entity that takes part in a session."""

    @property
    def entity_id(self) -> str:
        """Return the entity ID."""

    @property
    def speaker(self) -> Speaker:
        """Return the speaker of the display."""

    def check_request(self, media_id: str) -> None:
        """Raise ServiceValidationError if this speaker refuses the request."""

    def played(self, duration: float, delay: float) -> None:
        """Record a clip that started."""

    def play_error(self, err: Exception) -> Exception:
        """Turn an error into the one the action call raises."""


@dataclass(slots=True)
class _Session:
    media_id: str
    extra: dict[str, Any]
    volume: int | None
    target: str
    created: float = field(default_factory=time.monotonic)
    open: bool = True
    members: dict[str, Member] = field(default_factory=dict)
    results: dict[str, asyncio.Future[None]] = field(default_factory=dict)


class SyncManager:
    """Groups the play_media calls of one action and plays them together."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self._hass = hass
        self._sessions: dict[tuple[str, str, str], _Session] = {}
        # Displays of the running sessions with several displays; a session
        # that shares a display with one of them waits until it has started.
        self._busy: set[str] = set()
        self._busy_changed = asyncio.Condition()
        # Speaker entities by entity ID, for the speaker groups
        self.members: dict[str, Member] = {}

    async def async_play(
        self,
        context: Context | None,
        media_id: str,
        extra: dict[str, Any],
        volume: int | None,
        members: Sequence[Member],
    ) -> None:
        """Play on the members, together with the other calls of the action.

        A single member raises its own error. Several members (a speaker
        group) raise if any of them failed.
        """
        for member in members:
            member.check_request(media_id)
        session = self._join(context, media_id, extra, volume, members)
        results = await asyncio.gather(
            *(session.results[m.entity_id] for m in members), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if not errors:
            return
        if len(results) == 1:
            raise errors[0]
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="group_failed",
            translation_placeholders={
                "failed": str(len(errors)),
                "total": str(len(results)),
                "error": str(errors[0]),
            },
        ) from errors[0]

    def _join(
        self,
        context: Context | None,
        media_id: str,
        extra: dict[str, Any],
        volume: int | None,
        members: Sequence[Member],
    ) -> _Session:
        key = (
            None
            if context is None
            else (context.id, media_id, json.dumps(extra, sort_keys=True, default=str))
        )
        session = self._sessions.get(key) if key else None
        new = session is None or not session.open
        if new:
            session = _Session(media_id, extra, volume, members[0].entity_id)
            if key:
                self._sessions[key] = session
        assert session is not None
        for member in members:
            # A speaker named twice (directly and in a group) plays once
            if member.entity_id not in session.members:
                session.members[member.entity_id] = member
                result = self._hass.loop.create_future()
                # A caller that was cancelled meanwhile does not read its
                # result; mark it read so asyncio does not log it.
                result.add_done_callback(_consume)
                session.results[member.entity_id] = result
        if new:
            self._hass.async_create_background_task(
                self._async_run(session, key), f"{DOMAIN} play together"
            )
        return session

    async def _async_run(self, session: _Session, key: tuple[str, str, str] | None) -> None:
        fetch = self._hass.async_create_task(
            async_get_announcement(self._hass, session.media_id, session.target),
            f"{DOMAIN} fetch audio",
            eager_start=False,
        )
        try:
            await asyncio.sleep(JOIN_WINDOW)
            session.open = False
            if key is not None:
                self._sessions.pop(key, None)
            try:
                announcement = await fetch
            except Exception as err:
                # Every caller raises its own copy (tracebacks are per raise)
                for result in session.results.values():
                    result.set_exception(copy.copy(err))
                return
            if len(session.members) == 1:
                await self._async_play_together(session, announcement)
                return
            displays = set(session.members)
            async with self._busy_changed:
                await self._busy_changed.wait_for(lambda: not displays & self._busy)
                self._busy |= displays
            try:
                await self._async_play_together(session, announcement)
            finally:
                async with self._busy_changed:
                    self._busy -= displays
                    self._busy_changed.notify_all()
        finally:
            fetch.cancel()
            for result in session.results.values():
                if not result.done():
                    result.cancel()

    async def _async_play_together(self, session: _Session, announcement: Announcement) -> None:
        start = asyncio.Event()
        waiting = set(session.members)
        timer: asyncio.TimerHandle | None = None

        @callback
        def done_waiting(entity_id: str, ready: bool) -> None:
            nonlocal timer
            waiting.discard(entity_id)
            if not waiting:
                start.set()
            elif ready and timer is None:
                timer = self._hass.loop.call_later(SYNC_TIMEOUT, start.set)

        cache = session.extra.get("cache") is not False

        async def play(member: Member) -> None:
            speaker = member.speaker
            source = announcement.source if cache else None
            audio = ClipAudio(
                key=speaker.key_for(source) if source is not None else None,
                extension=announcement.extension,
                data=announcement.data,
                duration=announcement.duration,
            )
            try:
                async with speaker.turn():
                    if start.is_set():
                        raise SyncTimeoutError
                    prepared = await speaker.prepare(audio, session.volume)
                    if start.is_set():
                        await speaker.discard(audio, prepared)
                        raise SyncTimeoutError
                    done_waiting(member.entity_id, ready=True)
                    await start.wait()
                    duration = await speaker.start(audio, prepared)
            except Exception as err:
                done_waiting(member.entity_id, ready=False)
                session.results[member.entity_id].set_exception(member.play_error(err))
                return
            member.played(duration, round(time.monotonic() - session.created, 2))
            session.results[member.entity_id].set_result(None)

        try:
            await asyncio.gather(*(play(m) for m in session.members.values()))
        finally:
            if timer is not None:
                timer.cancel()


def _consume(result: asyncio.Future[None]) -> None:
    if not result.cancelled():
        result.exception()


@callback
def get_sync_manager(hass: HomeAssistant) -> SyncManager:
    """Return the manager shared by all displays."""
    if (manager := hass.data.get(DATA_SYNC)) is None:
        manager = hass.data[DATA_SYNC] = SyncManager(hass)
    return manager
