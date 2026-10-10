"""Privacy: diagnostics, file names, error messages, removal."""

from __future__ import annotations

import json
import re
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus.diagnostics import async_get_config_entry_diagnostics
from custom_components.wall_display_plus.speaker import cache_key, clip_filename

from .conftest import HOST, MAC, FakeDisplay

TTS = "media-source://tts/tts.piper?message=Die+Waschmaschine+ist+fertig&language=de_DE"


@pytest.fixture
def tts_audio():
    """Let the TTS engine return a few bytes of 'mp3'."""
    with patch(
        "custom_components.wall_display_plus.media_player.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", b"ID3fake-audio")),
    ) as mock:
        yield mock


def _eid(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "wall_display_plus", f"{MAC}_{key}")
    assert entity_id
    return entity_id


async def _play(hass: HomeAssistant, media_id: str) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: _eid(hass, MP_DOMAIN, "speaker"),
            ATTR_MEDIA_CONTENT_ID: media_id,
            ATTR_MEDIA_CONTENT_TYPE: "music",
        },
        blocking=True,
    )


# -- F1: diagnostics --


async def test_diagnostics_hold_nothing_personal(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """No address, MAC, keys, file names, timestamps or played titles."""

    async def status() -> dict[str, Any]:
        return {
            "playback": {
                "enable": True,
                "media_type": "AUDIO",
                "volume": 5,
                "media_meta": {"title": "Private Song", "artist": "Someone", "thumb": "data:x"},
            },
            "item_counts": {"audio": 1, "alert": 1},
        }

    display.get_media_status = status  # type: ignore[method-assign]
    await _play(hass, TTS)
    await setup_entry.runtime_data.coordinator.async_refresh()

    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    text = json.dumps(diag)

    assert HOST not in text
    assert MAC not in text and "02:a1" not in text
    for key in setup_entry.runtime_data.speaker.entries:
        assert key not in text
        assert key[:24] not in text
    assert "homeassistant_" not in text
    assert setup_entry.runtime_data.speaker.tag not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
    for private in ("Private Song", "Someone", "data:x", "Waschmaschine"):
        assert private not in text
    assert diag["cache"]["entries"] == 1
    assert diag["cache"]["newest_days"] == 0
    assert diag["clips_on_display"] == {"count": 1, "bytes": 13, "types": {"ALERT": 1}}
    assert diag["state"]["playback"] == {"enable": True, "media_type": "AUDIO", "volume": 5}


async def test_diagnostics_unreachable_display(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """While the display is away only the error type is shown."""
    display.reachable = False
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["clips_on_display"] == "unavailable: WallDisplayError"


# -- F2: secret in the file names --


def test_same_text_different_secret_different_name() -> None:
    """Without the secret, a file name tells nothing about the text."""
    name_a = clip_filename("abc123", cache_key(b"a" * 32, TTS), "mp3")
    name_b = clip_filename("abc123", cache_key(b"b" * 32, TTS), "mp3")
    assert name_a != name_b


async def test_secret_survives_reload(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """After a restart the same announcement is still a cache hit."""
    key_before = setup_entry.runtime_data.speaker.key_for(TTS)
    await _play(hass, TTS)
    await hass.config_entries.async_reload(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_entry.runtime_data.speaker.key_for(TTS) == key_before
    await _play(hass, TTS)
    assert len(display.uploads) == 1


async def test_clear_cache_creates_new_secret(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Clearing the cache makes old hashes useless."""
    speaker = setup_entry.runtime_data.speaker
    key_before = speaker.key_for(TTS)
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _eid(hass, BUTTON_DOMAIN, "clear_cache")},
        blocking=True,
    )
    assert speaker.key_for(TTS) != key_before


async def test_old_store_without_secret_is_migrated(
    hass: HomeAssistant,
    display: FakeDisplay,
    config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    tts_audio,
) -> None:
    """Clips cached under the old unkeyed names are removed by the next cleanup."""
    key = f"wall_display_plus.{config_entry.entry_id}.cache"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "entries": {
                "oldkey": {
                    "filename": "wdp_oldunkeyedname.mp3",
                    "last_used": dt_util.utcnow().isoformat(),
                }
            }
        },
    }
    await display.upload("wdp_oldunkeyedname.mp3", "ALERT", b"x")
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.runtime_data.speaker.entries == {}
    assert display.by_name("wdp_oldunkeyedname.mp3") is None  # startup cleanup
    await config_entry.runtime_data.speaker.async_flush()
    assert "secret" in hass_storage[key]["data"]


# -- F3: no URL query in error messages --


async def test_download_error_hides_query(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A failing download does not repeat the URL's token."""
    aioclient_mock.get("https://example.com/clip.mp3?token=geheim", status=403)
    with pytest.raises(HomeAssistantError) as err:
        await _play(hass, "https://example.com/clip.mp3?token=geheim")
    assert "geheim" not in str(err.value)
    assert "403" in str(err.value)


async def test_connection_error_hides_query(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Also a network error names the URL without its query."""
    aioclient_mock.get("https://example.com/clip.mp3?token=geheim", exc=TimeoutError())
    with pytest.raises(HomeAssistantError) as err:
        await _play(hass, "https://example.com/clip.mp3?token=geheim")
    assert "geheim" not in str(err.value)
    assert "https://example.com/clip.mp3" in str(err.value)


async def test_unsupported_media_hides_query(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """An unsupported media ID is named without its query."""
    with pytest.raises(HomeAssistantError) as err:
        await _play(hass, "ftp://example.com/a.mp3?token=geheim")
    assert "geheim" not in str(err.value)


async def test_tts_error_names_only_the_type(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A TTS engine's message could quote the text; only its type is passed on."""
    with (
        patch(
            "custom_components.wall_display_plus.media_player.tts.async_get_media_source_audio",
            AsyncMock(side_effect=RuntimeError("could not say 'Die Waschmaschine ist fertig'")),
        ),
        pytest.raises(HomeAssistantError) as err,
    ):
        await _play(hass, TTS)
    assert "Waschmaschine" not in str(err.value)
    assert "RuntimeError" in str(err.value)


# -- F4: removal --


async def test_removal_deletes_our_clips(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Removing the integration deletes our clips and nothing else."""
    await _play(hass, TTS)
    await display.upload("wdp_orphan.mp3", "ALERT", b"x")
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert display.clips() == []
    assert display.by_name("song.mp3") is not None
    assert display.by_name("digital_alarm.mp3") is not None


async def test_removal_works_when_display_is_away(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
    tts_audio,
) -> None:
    """An unreachable display does not block the removal."""
    await _play(hass, TTS)
    await setup_entry.runtime_data.speaker.async_flush()
    key = f"wall_display_plus.{setup_entry.entry_id}.cache"
    display.reachable = False
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
    assert len(display.clips()) == 1


async def test_removal_times_out(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A display that hangs is given up after the timeout."""

    async def hang(*args: Any, **kwargs: Any) -> list[Any]:
        raise TimeoutError

    display.list_media = hang  # type: ignore[method-assign]
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()


def test_unparsable_url_in_message() -> None:
    """A media ID that is no URL at all is described safely."""
    from custom_components.wall_display_plus.media_player import _without_query

    assert _without_query("http://[") == "<invalid URL>"
