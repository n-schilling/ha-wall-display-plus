"""File names on the display: prefix, per-instance tag, length limit."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.wall_display_plus.const import MAX_FILENAME_BYTES
from custom_components.wall_display_plus.speaker import clip_filename, owns

from .conftest import MAC, FakeDisplay

TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"
OTHER_INSTANCE = "homeassistant_ffffff_0123456789abcdef01234567.mp3"


@pytest.fixture
def tts_audio():
    """Let the TTS engine return a few bytes of 'mp3'."""
    with patch(
        "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", b"ID3fake-audio")),
    ) as mock:
        yield mock


def _eid(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "wall_display_plus", f"{MAC}_{key}")
    assert entity_id
    return entity_id


async def _play(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: _eid(hass, MP_DOMAIN, "speaker"),
            ATTR_MEDIA_CONTENT_ID: TTS,
            ATTR_MEDIA_CONTENT_TYPE: "music",
        },
        blocking=True,
    )


def test_longest_name_is_far_below_the_limit() -> None:
    """The longest possible name stays far below the 250 bytes the firmware stores."""
    extensions = ["mp3", "opus", "webm", "toolong", "ünï", "a/b", ""]
    names = [clip_filename("abcdef", "f" * 64, ext) for ext in extensions]
    assert max(len(name.encode()) for name in names) <= 50 < MAX_FILENAME_BYTES
    assert all(name.isascii() for name in names)


def test_owns() -> None:
    """Only files with our prefix and tag are ours."""
    assert owns("homeassistant_abcdef_x.mp3", "abcdef")
    assert not owns(OTHER_INSTANCE, "abcdef")
    assert not owns("homeassistant_abcdef_x.mp3", None)
    assert not owns("song.mp3", "abcdef")


async def test_other_instance_is_never_touched(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Clips of another Home Assistant on the same display survive cleanup, clear and removal."""
    await display.upload(OTHER_INSTANCE, "ALERT", b"x")
    await _play(hass)
    assert await setup_entry.runtime_data.speaker.async_cleanup() == 0
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _eid(hass, BUTTON_DOMAIN, "clear_cache")},
        blocking=True,
    )
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert [i.filename for i in display.clips()] == [OTHER_INSTANCE]


async def test_other_instance_is_not_evicted_for_room(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Even when room is needed, another instance's clips stay."""
    await display.upload(OTHER_INSTANCE, "ALERT", b"x" * 100)
    setup_entry.runtime_data.speaker.cache_max_bytes = 13
    await _play(hass)
    assert display.by_name(OTHER_INSTANCE) is not None


async def test_tag_is_stable(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    tts_audio,
) -> None:
    """The tag survives a restart and clearing the cache; only the secret changes."""
    speaker = setup_entry.runtime_data.speaker
    tag = speaker.tag
    assert len(tag) == 6
    await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.runtime_data.speaker.tag == tag
    await setup_entry.runtime_data.speaker.async_clear()
    assert setup_entry.runtime_data.speaker.tag == tag
    await setup_entry.runtime_data.speaker.async_flush()
    key = f"wall_display_plus.{setup_entry.entry_id}.cache"
    assert hass_storage[key]["data"]["tag"] == tag
