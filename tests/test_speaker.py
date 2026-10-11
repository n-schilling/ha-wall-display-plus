"""Tests for the speaker: playing, caching and cleaning up clips."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch
from urllib.parse import quote_plus

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import MAC, FakeDisplay, own_file

TTS_HELLO = "media-source://tts/tts.piper?message=Hallo&language=de_DE&cache=true"
TTS_HELLO_NOCACHE_FLAG = "media-source://tts/tts.piper?message=Hallo&language=de_DE&cache=false"
TTS_OTHER = "media-source://tts/tts.piper?message=Tsch%C3%BCss&language=de_DE"


@pytest.fixture
def tts_audio():
    """Let the TTS engine return a few bytes of 'mp3'."""
    with patch(
        "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", b"ID3fake-audio")),
    ) as mock:
        yield mock


def _speaker_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        MP_DOMAIN, "wall_display_plus", f"{MAC}_speaker"
    )
    assert entity_id
    return entity_id


async def _play(hass: HomeAssistant, media_id: str, extra: dict | None = None) -> None:
    data = {
        ATTR_ENTITY_ID: _speaker_id(hass),
        ATTR_MEDIA_CONTENT_ID: media_id,
        ATTR_MEDIA_CONTENT_TYPE: "music",
    }
    if extra is not None:
        data[ATTR_MEDIA_EXTRA] = extra
    await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True)


async def test_first_play_uploads_alert_clip(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The first announcement is uploaded as an alert clip and played once."""
    await _play(hass, TTS_HELLO)
    assert len(display.uploads) == 1
    filename, media_type, size = display.uploads[0]
    tag = setup_entry.runtime_data.speaker.tag
    assert filename.startswith(f"homeassistant_{tag}_") and filename.endswith(".mp3")
    assert media_type == "ALERT"
    assert size == len(b"ID3fake-audio")
    assert display.played == [display.by_name(filename).id]


async def test_cache_hit_skips_upload(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The same announcement again plays the stored clip without uploading."""
    await _play(hass, TTS_HELLO)
    await _play(hass, TTS_HELLO)
    # The TTS cache flag does not change what is said: same clip
    await _play(hass, TTS_HELLO_NOCACHE_FLAG)
    assert len(display.uploads) == 1
    assert len(display.played) == 3
    assert len(set(display.played)) == 1


async def test_different_text_is_a_different_clip(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Another message gets its own clip."""
    await _play(hass, TTS_HELLO)
    await _play(hass, TTS_OTHER)
    assert len(display.uploads) == 2
    assert len(display.clips()) == 2


async def test_clip_gone_is_uploaded_again(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A clip deleted on the display is uploaded again as if never cached."""
    await _play(hass, TTS_HELLO)
    (clip,) = display.clips()
    del display.library[clip.id]
    await _play(hass, TTS_HELLO)
    assert len(display.uploads) == 2
    assert display.played[-1] == display.by_name(clip.filename).id


async def test_clip_with_new_id_is_found_by_name(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The file name counts, not the id the display once gave it."""
    await _play(hass, TTS_HELLO)
    (clip,) = display.clips()
    new_id = display.renumber(clip.filename)
    await _play(hass, TTS_HELLO)
    assert len(display.uploads) == 1
    assert display.played[-1] == new_id


async def test_cache_false_deletes_after_playing(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """extra: {cache: false} plays once and removes the clip afterwards."""
    await _play(hass, TTS_HELLO, {"cache": False})
    assert len(display.uploads) == 1
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.clips() == []
    assert setup_entry.runtime_data.speaker.entries == {}


async def test_expired_and_orphaned_clips_are_deleted(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Cleanup removes clips unused for cache_days and our unknown files only."""
    await _play(hass, TTS_HELLO)
    await _play(hass, TTS_OTHER)
    speaker = setup_entry.runtime_data.speaker
    old_key = next(
        key
        for key, entry in speaker.entries.items()
        if display.by_name(entry["filename"]).id == display.played[0]
    )
    speaker.entries[old_key]["last_used"] = (dt_util.utcnow() - timedelta(days=91)).isoformat()
    await display.upload(own_file(setup_entry, "orphan.mp3"), "ALERT", b"x")

    deleted = await speaker.async_cleanup()

    assert deleted == 2
    names = {item.filename for item in display.library.values()}
    assert own_file(setup_entry, "orphan.mp3") not in names
    assert {"digital_alarm.mp3", "song.mp3"} <= names  # foreign files untouched
    assert len(display.clips()) == 1
    assert old_key not in speaker.entries


async def test_play_url(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A plain URL is downloaded and cached under that URL."""
    aioclient_mock.get(
        "http://example.test/ding.wav",
        content=b"RIFFfake",
        headers={"Content-Type": "audio/x-wav"},
    )
    await _play(hass, "http://example.test/ding.wav")
    await _play(hass, "http://example.test/ding.wav")
    assert len(display.uploads) == 1
    assert display.uploads[0][0].endswith(".wav")
    assert aioclient_mock.call_count == 2  # downloaded each time, uploaded once


async def test_display_error_is_reported(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A failing display raises a readable error."""
    display.reachable = False
    with pytest.raises(HomeAssistantError, match="could not play"):
        await _play(hass, TTS_HELLO)


async def test_cache_survives_restart(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The cache is stored and still known after reloading the entry."""
    await _play(hass, TTS_HELLO)
    await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    await _play(hass, TTS_HELLO)
    assert len(display.uploads) == 1


async def test_relative_path_is_refused_and_not_signed(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A path of this instance is never fetched with a signature (no SSRF)."""
    with (
        patch("custom_components.wall_display_plus.audio.async_process_play_media_url") as sign,
        pytest.raises(HomeAssistantError, match="Only text to speech"),
    ):
        await _play(hass, "/api/config/core/check_config")
    sign.assert_not_called()
    assert aioclient_mock.call_count == 0
    assert display.uploads == []


async def test_other_scheme_is_refused(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """file:// and similar are refused."""
    with pytest.raises(HomeAssistantError, match="Only text to speech"):
        await _play(hass, "file:///config/secrets.yaml")
    assert display.uploads == []


async def test_non_audio_download_is_refused(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A URL that returns something else than audio is not uploaded."""
    aioclient_mock.get(
        "http://example.test/page",
        content=b'{"secret": 1}',
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HomeAssistantError, match="not audio"):
        await _play(hass, "http://example.test/page")
    assert display.uploads == []


def _tts_message(text: str) -> str:
    return f"media-source://tts/tts.piper?message={quote_plus(text)}&language=de_DE"


async def test_text_too_long_is_refused_before_the_engine_runs(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A text over the limit (default 500) never reaches the TTS engine."""
    with pytest.raises(
        ServiceValidationError, match="501 characters; this speaker allows at most 500"
    ):
        await _play(hass, _tts_message("x" * 501))
    tts_audio.assert_not_called()
    assert display.uploads == []


async def test_text_at_the_limit_is_spoken(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Exactly the maximum length is fine; umlauts count as one character."""
    await _play(hass, _tts_message("ä" * 500))
    tts_audio.assert_called_once()


async def test_text_limit_follows_the_option(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The limit comes from the options."""
    hass.config_entries.async_update_entry(setup_entry, options={"max_text_length": 50})
    await hass.async_block_till_done()
    with pytest.raises(ServiceValidationError, match="at most 50"):
        await _play(hass, _tts_message("x" * 51))
    await _play(hass, _tts_message("x" * 50))
    assert len(display.uploads) == 1
