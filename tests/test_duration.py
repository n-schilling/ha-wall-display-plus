"""Clip length: the display does not report it for alert clips (firmware 2.8.2)."""

from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, patch
import wave

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
from homeassistant.helpers import entity_registry as er

from custom_components.wall_display_plus.audio import audio_duration

from .conftest import MAC, FakeDisplay

TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"


def _wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0\0" * int(rate * seconds))
    return buffer.getvalue()


def test_length_is_read_from_the_audio() -> None:
    """A readable file gives its real length."""
    assert audio_duration(_wav(1.5)) == pytest.approx(1.5)


def test_unreadable_audio_is_estimated() -> None:
    """Unreadable audio is estimated from its size, on the long side."""
    assert audio_duration(b"ID3broken" + b"\0" * 3991) == pytest.approx(1.0)
    assert audio_duration(b"x" * 4000) == pytest.approx(1.0)


async def test_volume_goes_back_after_the_measured_length(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Without a duration from the display, the measured length counts.

    The volume of the announcement stays until the clip has played, and a
    second announcement waits for the first.
    """
    display.reports_duration = False
    entity_id = er.async_get(hass).async_get_entity_id(
        MP_DOMAIN, "wall_display_plus", f"{MAC}_speaker"
    )
    with patch(
        "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
        AsyncMock(return_value=("wav", _wav(0.4))),
    ):
        data = {
            ATTR_ENTITY_ID: entity_id,
            ATTR_MEDIA_CONTENT_ID: TTS,
            ATTR_MEDIA_CONTENT_TYPE: "music",
        }
        await hass.services.async_call(
            MP_DOMAIN,
            SERVICE_PLAY_MEDIA,
            {**data, ATTR_MEDIA_EXTRA: {"volume": 0.9}},
            blocking=True,
        )
        assert display.volume_writes == [9]
        assert setup_entry.runtime_data.speaker.last_delay is not None
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert display.volume_writes == [9]  # still playing

        await hass.services.async_call(MP_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True)
    assert display.volume_writes == [9, 7]
    first, second = display.played_at
    assert second - first >= 0.4
