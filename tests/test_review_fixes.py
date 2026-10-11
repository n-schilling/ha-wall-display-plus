"""Fixes from the review of 0.1.4: stop, cleanup, state, CPU, language, downloads."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_MEDIA_STOP,
    STATE_IDLE,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus.api import (
    WallDisplayAuthError,
    WallDisplayUnsupportedError,
)
from custom_components.wall_display_plus.button import _engine_language
from custom_components.wall_display_plus.const import UPDATE_INTERVAL

from .conftest import MAC, FakeDisplay, own_file

TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"


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


async def _play(hass: HomeAssistant, extra: dict[str, Any] | None = None) -> None:
    data: dict[str, Any] = {
        ATTR_ENTITY_ID: _eid(hass, MP_DOMAIN, "speaker"),
        ATTR_MEDIA_CONTENT_ID: TTS,
        ATTR_MEDIA_CONTENT_TYPE: "music",
    }
    if extra is not None:
        data[ATTR_MEDIA_EXTRA] = extra
    await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True)


async def _stop(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_MEDIA_STOP,
        {ATTR_ENTITY_ID: _eid(hass, MP_DOMAIN, "speaker")},
        blocking=True,
    )


async def test_stop_frees_the_display_and_the_volume(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """After stop, the next announcement does not wait and the volume goes back at once."""
    display.clip_duration = 180
    await _play(hass, {"volume": 0.9})
    await _stop(hass)
    await hass.async_block_till_done()
    assert display.volume_writes == [9, 7]

    sleeps: list[float] = []
    real_sleep = __import__("asyncio").sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    with patch("custom_components.wall_display_plus.speaker.asyncio.sleep", fake_sleep):
        await _play(hass)
    assert not [s for s in sleeps if s > 1]
    assert len(display.played) == 2


async def test_cleanup_keeps_an_uncached_clip_still_to_play(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The cleanup does not delete an uncached clip that is still playing; orphans go."""
    display.clip_duration = 30
    await display.upload(own_file(setup_entry, "orphan.mp3"), "ALERT", b"x")
    await _play(hass, {"cache": False})
    assert await setup_entry.runtime_data.speaker.async_cleanup() == 1
    assert len(display.clips()) == 1  # the playing uncached clip
    assert display.by_name(own_file(setup_entry, "orphan.mp3")) is None


async def test_clip_end_does_not_touch_the_library(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """At the end of a clip only the state changes; expired clips go in the daily run."""
    await _play(hass)
    await hass.async_block_till_done()
    await asyncio.sleep(0.1)  # the simulated clip lasts 50 ms of real time
    calls = display.list_calls
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()
    assert display.list_calls == calls
    assert hass.states.get(_eid(hass, MP_DOMAIN, "speaker")).state == STATE_IDLE


async def test_status_read_during_our_clip_does_not_stick(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A poll that saw our clip playing does not keep the speaker playing afterwards."""

    async def playing() -> dict[str, Any]:
        return {"playback": {"enable": True, "media_type": "ALERT", "volume": 7}}

    display.clip_duration = 30
    display.get_media_status = playing  # type: ignore[method-assign]
    await _play(hass)
    await setup_entry.runtime_data.coordinator.async_refresh()
    entity_id = _eid(hass, MP_DOMAIN, "speaker")
    assert hass.states.get(entity_id).state == STATE_PLAYING
    await _stop(hass)
    assert hass.states.get(entity_id).state == STATE_IDLE
    # A later poll that still sees an alert: one started elsewhere
    await setup_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_PLAYING


async def test_cpu_temperature_only_when_enabled(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The disabled CPU sensor costs no request."""
    await setup_entry.runtime_data.coordinator.async_refresh()
    assert display.cpu_calls == 0


async def test_cpu_temperature_failure_is_no_outage(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A firmware without the temperature keeps the display available."""
    entity_id = _eid(hass, "sensor", "cpu_temperature")
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()

    async def unsupported() -> float | None:
        raise WallDisplayUnsupportedError("Sys.GetInternalTemperatures: -114")

    display.get_cpu_temperature = unsupported  # type: ignore[method-assign]
    async_fire_time_changed(hass, dt_util.utcnow() + UPDATE_INTERVAL + timedelta(seconds=40))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "unknown"
    assert hass.states.get(_eid(hass, MP_DOMAIN, "speaker")).state != STATE_UNAVAILABLE

    async def denied() -> float | None:
        raise WallDisplayAuthError("The display rejected the password")

    display.get_cpu_temperature = denied  # type: ignore[method-assign]
    async_fire_time_changed(hass, dt_util.utcnow() + 2 * UPDATE_INTERVAL + timedelta(seconds=40))
    await hass.async_block_till_done()
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


def test_engine_without_default_language(hass: HomeAssistant) -> None:
    """An engine without fitting or default language gets the wanted one."""
    engine = SimpleNamespace(supported_languages=["en_US"], default_language=None)
    with patch(
        "custom_components.wall_display_plus.button.get_engine_instance", return_value=engine
    ):
        assert _engine_language(hass, "tts.x", "de") == "de"


async def test_broken_content_length(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A broken Content-Length header does not break the download."""
    aioclient_mock.get(
        "http://192.0.2.1/ding.mp3",
        content=b"ID3",
        headers={"Content-Type": "audio/mpeg", "Content-Length": "abc"},
    )
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: _eid(hass, MP_DOMAIN, "speaker"),
            ATTR_MEDIA_CONTENT_ID: "http://192.0.2.1/ding.mp3",
            ATTR_MEDIA_CONTENT_TYPE: "music",
        },
        blocking=True,
    )
    assert display.played
