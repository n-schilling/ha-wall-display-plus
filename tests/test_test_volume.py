"""The button that speaks a test sentence at the announcement volume."""

from __future__ import annotations

from collections.abc import Generator
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.wall_display_plus.sync import get_sync_manager

from .conftest import MAC, FakeDisplay

TTS = "media-source://tts/tts.piper?message=Test&language=de_DE"


@pytest.fixture
def tts_engine() -> Generator[MagicMock]:
    """A text-to-speech engine that returns a few bytes of 'mp3'."""
    with (
        patch(
            "custom_components.wall_display_plus.button.tts.async_default_engine",
            return_value="tts.default",
        ),
        patch(
            "custom_components.wall_display_plus.button.get_engine_instance",
            return_value=None,
        ),
        patch(
            "custom_components.wall_display_plus.button.tts.generate_media_source_id",
            return_value=TTS,
        ) as generate,
        patch(
            "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
            AsyncMock(return_value=("mp3", b"ID3fake-audio")),
        ),
    ):
        yield generate


async def _press(hass: HomeAssistant) -> None:
    entity_id = er.async_get(hass).async_get_entity_id(
        BUTTON_DOMAIN, "wall_display_plus", f"{MAC}_test_volume"
    )
    assert entity_id
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


@pytest.mark.parametrize(
    ("language", "message"),
    [
        ("de", "Das ist ein Test der Ansage-Lautstärke."),
        ("en", "This is a test of the announcement volume."),
        ("fr", "This is a test of the announcement volume."),
    ],
)
async def test_speaks_in_the_language_of_home_assistant(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    tts_engine: MagicMock,
    language: str,
    message: str,
) -> None:
    """Without a voice assistant, the default engine speaks in HA's language."""
    hass.config.language = language
    display.volume = 3
    await _press(hass)
    tts_engine.assert_called_once_with(
        hass, message, engine="tts.default", language=language, options=None
    )
    assert len(display.played) == 1
    assert display.volume_writes[0] == 7  # the announcement volume


@pytest.mark.parametrize(
    ("pipeline", "expected"),
    [
        (
            SimpleNamespace(tts_engine="tts.piper", tts_language="de-DE", tts_voice="thorsten"),
            (
                "Das ist ein Test der Ansage-Lautstärke.",
                "tts.piper",
                "de-DE",
                {"voice": "thorsten"},
            ),
        ),
        (
            SimpleNamespace(tts_engine="tts.piper", tts_language=None, tts_voice=None),
            ("This is a test of the announcement volume.", "tts.piper", "en", None),
        ),
        (
            SimpleNamespace(tts_engine=None, tts_language=None, tts_voice=None),
            ("This is a test of the announcement volume.", "tts.default", "en", None),
        ),
    ],
)
async def test_uses_the_voice_assistant(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    tts_engine: MagicMock,
    pipeline: SimpleNamespace,
    expected: tuple,
) -> None:
    """The preferred voice assistant's voice speaks, if it has one."""
    hass.config.language = "en"
    module = ModuleType("homeassistant.components.assist_pipeline.pipeline")
    module.async_get_pipeline = lambda _hass: pipeline  # type: ignore[attr-defined]
    hass.config.components.add("assist_pipeline")
    with patch.dict(sys.modules, {module.__name__: module}):
        await _press(hass)
    message, engine, language, options = expected
    tts_engine.assert_called_once_with(
        hass, message, engine=engine, language=language, options=options
    )


@pytest.mark.parametrize(
    ("language", "engine_language", "message"),
    [
        ("de", "de_DE", "Das ist ein Test der Ansage-Lautstärke."),
        ("en", "en_US", "This is a test of the announcement volume."),
        ("fr", "de_DE", "Das ist ein Test der Ansage-Lautstärke."),
        ("de_DE", "de_DE", "Das ist ein Test der Ansage-Lautstärke."),
    ],
)
async def test_language_the_engine_knows(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    tts_engine: MagicMock,
    language: str,
    engine_language: str,
    message: str,
) -> None:
    """HA's "de" becomes the engine's "de_DE"; unknown ones its default language."""
    hass.config.language = language
    engine = SimpleNamespace(supported_languages=["de_DE", "en_US"], default_language="de_DE")
    with patch(
        "custom_components.wall_display_plus.button.get_engine_instance", return_value=engine
    ):
        await _press(hass)
    tts_engine.assert_called_once_with(
        hass, message, engine="tts.default", language=engine_language, options=None
    )


async def test_without_engine(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Without any text-to-speech engine the button says so."""
    with (
        patch(
            "custom_components.wall_display_plus.button.tts.async_default_engine",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError, match="No text-to-speech engine"),
    ):
        await _press(hass)
    assert not display.played


async def test_speaker_unavailable(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_engine
) -> None:
    """If the speaker entity is gone, the button says so."""
    get_sync_manager(hass).members.clear()
    with pytest.raises(HomeAssistantError, match="speaker of this display is not available"):
        await _press(hass)
