"""Tests for the cache limit and the free storage check."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus.const import FREE_SPACE_RESERVE

from .conftest import MAC, FakeDisplay, own_file

AUDIO = b"ID3fake-audio"  # 13 bytes per clip


@pytest.fixture
def tts_audio():
    """Let the TTS engine return a few bytes of 'mp3'."""
    with patch(
        "custom_components.wall_display_plus.media_player.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", AUDIO)),
    ) as mock:
        yield mock


def _tts(message: str) -> str:
    return f"media-source://tts/tts.piper?message={message}&language=de_DE"


async def _play(hass: HomeAssistant, media_id: str) -> None:
    entity_id = er.async_get(hass).async_get_entity_id(
        MP_DOMAIN, "wall_display_plus", f"{MAC}_speaker"
    )
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: entity_id,
            ATTR_MEDIA_CONTENT_ID: media_id,
            ATTR_MEDIA_CONTENT_TYPE: "music",
        },
        blocking=True,
    )


def _age(entry: MockConfigEntry, display: FakeDisplay, message_index: int, days: int) -> None:
    """Make the clip uploaded as number message_index look last played days ago."""
    filename = display.uploads[message_index][0]
    for cache_entry in entry.runtime_data.speaker.entries.values():
        if cache_entry["filename"] == filename:
            cache_entry["last_used"] = (dt_util.utcnow() - timedelta(days=days)).isoformat()


async def test_default_limit_is_500_mb(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Without options the cache may take 500 MB."""
    assert setup_entry.runtime_data.speaker.cache_max_bytes == 500 * 1024 * 1024


async def test_limit_evicts_least_recently_played(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """When the cache is full, the clip played longest ago goes first."""
    setup_entry.runtime_data.speaker.cache_max_bytes = 3 * len(AUDIO)
    for message in ("a", "b", "c"):
        await _play(hass, _tts(message))
    _age(setup_entry, display, 0, days=1)  # a: yesterday
    _age(setup_entry, display, 1, days=3)  # b: longest ago
    _age(setup_entry, display, 2, days=2)  # c
    names = [u[0] for u in display.uploads]

    await _play(hass, _tts("d"))

    remaining = {i.filename for i in display.clips()}
    assert names[1] not in remaining  # b evicted
    assert {names[0], names[2], display.uploads[3][0]} == remaining
    assert len(setup_entry.runtime_data.speaker.entries) == 3


async def test_orphans_are_evicted_before_cached_clips(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Our clips the cache does not know are removed first."""
    setup_entry.runtime_data.speaker.cache_max_bytes = 2 * len(AUDIO)
    await _play(hass, _tts("a"))
    await display.upload(own_file(setup_entry, "orphan.mp3"), "ALERT", AUDIO)
    await _play(hass, _tts("b"))
    assert display.by_name(own_file(setup_entry, "orphan.mp3")) is None
    assert len(display.clips()) == 2


async def test_clip_larger_than_cache_is_not_kept(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A clip bigger than the whole cache plays once and is deleted, the cache stays."""
    await _play(hass, _tts("small"))
    setup_entry.runtime_data.speaker.cache_max_bytes = len(AUDIO) - 1
    await _play(hass, _tts("too big"))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(display.played) == 2
    assert [i.filename for i in display.clips()] == [display.uploads[0][0]]
    assert len(setup_entry.runtime_data.speaker.entries) == 1


async def test_low_storage_evicts_our_clips(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """With little free storage, our old clips make room for the new one."""
    await _play(hass, _tts("a"))
    await _play(hass, _tts("b"))
    display.fs_free = FREE_SPACE_RESERVE + len(AUDIO) - 1  # one clip short
    await _play(hass, _tts("c"))
    assert len(display.clips()) == 2
    assert display.uploads[0][0] not in {i.filename for i in display.clips()}
    assert display.fs_free >= FREE_SPACE_RESERVE


async def test_storage_full_raises_and_creates_issue(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """When even deleting all our clips is not enough, nothing is uploaded."""
    await _play(hass, _tts("a"))
    display.fs_free = 50 * 1024 * 1024
    uploads_before = len(display.uploads)
    with pytest.raises(HomeAssistantError, match="too little free storage"):
        await _play(hass, _tts("b"))
    assert len(display.uploads) == uploads_before
    assert display.clips() == []  # it tried: our clip was deleted
    issue_id = f"storage_full_{setup_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue("wall_display_plus", issue_id)
    assert issue is not None
    assert issue.translation_placeholders["reserve_mb"] == "100"

    display.fs_free = 10 * 1024**3  # the user freed storage
    await _play(hass, _tts("b"))
    assert ir.async_get(hass).async_get_issue("wall_display_plus", issue_id) is None


async def test_foreign_files_are_never_evicted(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Music and ringtones stay, even when storage is short."""
    display.fs_free = 1024
    with pytest.raises(HomeAssistantError):
        await _play(hass, _tts("a"))
    assert display.by_name("song.mp3") is not None
    assert display.by_name("digital_alarm.mp3") is not None


async def test_unknown_free_storage_is_not_checked(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A firmware that does not report free storage still plays."""
    display.fs_free = None
    await _play(hass, _tts("a"))
    assert len(display.played) == 1


async def test_cached_clip_needs_no_room(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Replaying a cached clip neither checks storage nor evicts."""
    await _play(hass, _tts("a"))
    display.fs_free = 0
    await _play(hass, _tts("a"))
    assert len(display.played) == 2


async def test_failed_eviction_is_skipped(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A clip that cannot be deleted does not count as freed."""
    setup_entry.runtime_data.speaker.cache_max_bytes = len(AUDIO)
    await _play(hass, _tts("a"))
    original = display.delete_media

    async def refuse(media_id: int) -> None:
        from custom_components.wall_display_plus.api import WallDisplayError

        raise WallDisplayError("busy")

    display.delete_media = refuse  # type: ignore[method-assign]
    await _play(hass, _tts("b"))  # over the limit for a moment, but it plays
    assert len(display.played) == 2
    display.delete_media = original  # type: ignore[method-assign]
