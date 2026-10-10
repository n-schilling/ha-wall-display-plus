"""Error paths: setup, polling, playback, repair issue and the other platforms."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_MEDIA_STOP,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    STATE_IDLE,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus.api import (
    WallDisplayAuthError,
    WallDisplayError,
    WallDisplayUnsupportedError,
)
from custom_components.wall_display_plus.diagnostics import async_get_config_entry_diagnostics
from custom_components.wall_display_plus.speaker import clip_filename

from .conftest import MAC, FakeDisplay, own_file

TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"


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


def _fail(display: FakeDisplay, method: str, error: Exception) -> None:
    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise error

    setattr(display, method, failing)


# -- setup and polling --


async def test_setup_wrong_device(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """A different display at the address is not taken over silently."""
    config_entry = MockConfigEntry(
        domain=config_entry.domain,
        unique_id="aabbccddeeff",
        title=config_entry.title,
        data=dict(config_entry.data),
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "different display" in str(config_entry.reason)


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """A rejected password at setup asks for a new one."""
    display.auth_enabled = True
    display.password = "secret"
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_polling_failure_makes_entities_unavailable(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """When the display stops answering, the entities become unavailable and come back."""
    entity_id = _eid(hass, SWITCH_DOMAIN, "motion_wakes_screen")
    display.reachable = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    display.reachable = True
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=62))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != STATE_UNAVAILABLE


async def test_daily_cleanup_handles_errors(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The daily cleanup survives an unreachable display and starts reauth on 401."""
    display.reachable = False
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done()
    display.reachable = True
    _fail(display, "list_media", WallDisplayAuthError("no"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=50))
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert SOURCE_REAUTH in [f["context"]["source"] for f in flows]


async def test_daily_cleanup_deletes_orphans(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The daily cleanup removes clips nobody knows."""
    await display.upload(own_file(setup_entry, "orphan.mp3"), "ALERT", b"x")
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=25))
    await hass.async_block_till_done()
    assert display.by_name(own_file(setup_entry, "orphan.mp3")) is None


async def test_remove_entry_deletes_store(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, hass_storage
) -> None:
    """Deleting the entry removes the stored cache."""
    await setup_entry.runtime_data.speaker.async_flush()
    key = f"wall_display_plus.{setup_entry.entry_id}.cache"
    assert key in hass_storage
    await hass.config_entries.async_remove(setup_entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


# -- playback --


async def test_play_auth_error_starts_reauth(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A rejected password while playing asks for a new one."""
    _fail(display, "play_alert", WallDisplayAuthError("no"))
    with pytest.raises(HomeAssistantError, match="rejected the password"):
        await _play(hass, TTS)
    flows = hass.config_entries.flow.async_progress()
    assert SOURCE_REAUTH in [f["context"]["source"] for f in flows]


async def test_unsupported_firmware_creates_and_clears_issue(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """An unknown method creates a repair issue; the next good play removes it."""
    original = display.play_alert
    _fail(display, "play_alert", WallDisplayUnsupportedError("Method not found!"))
    with pytest.raises(HomeAssistantError, match="firmware"):
        await _play(hass, TTS)
    issue_id = f"unsupported_firmware_{setup_entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue("wall_display_plus", issue_id)
    assert issue is not None
    assert issue.translation_placeholders["firmware"] == "2.8.2"
    display.play_alert = original  # type: ignore[method-assign]
    await _play(hass, TTS)
    assert ir.async_get(hass).async_get_issue("wall_display_plus", issue_id) is None


async def test_tts_engine_failure(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A failing TTS engine is reported as such."""
    with (
        patch(
            "custom_components.wall_display_plus.media_player.tts.async_get_media_source_audio",
            AsyncMock(side_effect=RuntimeError("engine down")),
        ),
        pytest.raises(HomeAssistantError, match="text-to-speech engine failed"),
    ):
        await _play(hass, TTS)
    with (
        patch(
            "custom_components.wall_display_plus.media_player.tts.async_get_media_source_audio",
            AsyncMock(side_effect=HomeAssistantError("as is")),
        ),
        pytest.raises(HomeAssistantError, match="as is"),
    ):
        await _play(hass, TTS)


async def test_tts_stream_is_resolved_and_not_cached(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A one-time TTS stream of the voice assistant is resolved, played once, not kept."""
    item = MagicMock(url="/api/tts_proxy/abc123.mp3", mime_type="audio/mpeg")
    aioclient_mock.get(
        "http://example.local/api/tts_proxy/abc123.mp3",
        content=b"ID3",
        headers={"Content-Type": "audio/mpeg"},
    )
    with (
        patch(
            "custom_components.wall_display_plus.media_player.media_source.async_resolve_media",
            AsyncMock(return_value=item),
        ),
        patch(
            "custom_components.wall_display_plus.media_player.async_process_play_media_url",
            return_value="http://example.local/api/tts_proxy/abc123.mp3",
        ),
    ):
        await _play(hass, "media-source://tts/-stream-/abc123")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(display.uploads) == 1
    assert display.clips() == []


async def test_state_playing_and_stop(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The speaker is playing while a clip runs, and stop ends it."""
    entity_id = _eid(hass, MP_DOMAIN, "speaker")
    assert hass.states.get(entity_id).state == STATE_IDLE
    display.clip_duration = 30
    await _play(hass, TTS)
    assert hass.states.get(entity_id).state == STATE_PLAYING
    await hass.services.async_call(
        MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert hass.states.get(entity_id).state == STATE_IDLE
    _fail(display, "stop", WallDisplayError("down"))
    with pytest.raises(HomeAssistantError, match="could not be stopped"):
        await hass.services.async_call(
            MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )


async def test_state_from_polling(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """An alert started elsewhere shows as playing."""
    entity_id = _eid(hass, MP_DOMAIN, "speaker")

    async def status() -> dict[str, Any]:
        return {"playback": {"enable": True, "media_type": "ALERT"}}

    display.get_media_status = status  # type: ignore[method-assign]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_PLAYING


async def test_media_source_audio(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Other media sources are resolved, signed by Home Assistant and downloaded."""
    play_item = MagicMock(url="/media/local/ding.mp3", mime_type="audio/mpeg")
    aioclient_mock.get("http://example.local/media/local/ding.mp3?authSig=x", content=b"ID3")
    with (
        patch(
            "custom_components.wall_display_plus.media_player.media_source.async_resolve_media",
            AsyncMock(return_value=play_item),
        ),
        patch(
            "custom_components.wall_display_plus.media_player.async_process_play_media_url",
            return_value="http://example.local/media/local/ding.mp3?authSig=x",
        ) as sign,
    ):
        aioclient_mock.clear_requests()
        aioclient_mock.get(
            "http://example.local/media/local/ding.mp3?authSig=x",
            content=b"ID3",
            headers={"Content-Type": "audio/mpeg"},
        )
        await _play(hass, "media-source://media_source/local/ding.mp3")
    sign.assert_called_once()
    assert display.uploads[0][0].endswith(".mp3")


async def test_media_source_not_audio(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A picture from a media source is refused before downloading."""
    with (
        patch(
            "custom_components.wall_display_plus.media_player.media_source.async_resolve_media",
            AsyncMock(return_value=MagicMock(url="/media/local/a.jpg", mime_type="image/jpeg")),
        ),
        pytest.raises(ServiceValidationError, match="not audio"),
    ):
        await _play(hass, "media-source://media_source/local/a.jpg")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"status": 500}, "could not be downloaded"),
        ({"exc": TimeoutError()}, "could not be downloaded"),
        ({"headers": {"Content-Type": "audio/mpeg", "Content-Length": "999999999"}}, "20 MB"),
    ],
)
async def test_download_errors(
    hass: HomeAssistant,
    aioclient_mock,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    kwargs: dict[str, Any],
    match: str,
) -> None:
    """Download problems are reported, nothing is uploaded."""
    aioclient_mock.get("http://example.test/a.mp3", **kwargs)
    with pytest.raises(HomeAssistantError, match=match):
        await _play(hass, "http://example.test/a.mp3")
    assert display.uploads == []


async def test_download_too_large_without_length(
    hass: HomeAssistant, aioclient_mock, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A body larger than the limit is refused even without Content-Length."""
    aioclient_mock.get(
        "http://example.test/big.mp3", content=b"x" * 64, headers={"Content-Type": "audio/mpeg"}
    )
    with (
        patch("custom_components.wall_display_plus.media_player.MAX_CLIP_BYTES", 10),
        pytest.raises(HomeAssistantError, match="20 MB"),
    ):
        await _play(hass, "http://example.test/big.mp3")


async def test_upload_not_listed(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A clip that does not appear after the upload is an error."""

    async def nothing(*args: Any, **kwargs: Any) -> None:
        return None

    display.find_media = nothing  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError, match="not in the media library"):
        await _play(hass, TTS)


async def test_announcements_wait_for_each_other(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A second announcement waits until the first has finished."""
    display.clip_duration = 0.2
    sleeps: list[float] = []
    real_sleep = __import__("asyncio").sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    with patch("custom_components.wall_display_plus.speaker.asyncio.sleep", fake_sleep):
        await _play(hass, TTS)
        await _play(hass, TTS)
    assert sleeps and 0 < sleeps[0] <= 0.2


async def test_uncached_delete_failure_is_ignored(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """If the uncached clip is already gone, nothing breaks."""
    await hass.services.async_call(
        MP_DOMAIN,
        SERVICE_PLAY_MEDIA,
        {
            ATTR_ENTITY_ID: _eid(hass, MP_DOMAIN, "speaker"),
            ATTR_MEDIA_CONTENT_ID: TTS,
            ATTR_MEDIA_CONTENT_TYPE: "music",
            "extra": {"cache": False},
        },
        blocking=True,
    )
    _fail(display, "delete_media", WallDisplayError("gone"))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_cleanup_after_clip_survives_errors(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The cleanup after a clip may fail without consequences."""
    await _play(hass, TTS)
    _fail(display, "list_media", WallDisplayError("down"))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=5))
    await hass.async_block_till_done()


async def test_cleanup_skips_files_that_vanished(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A file deleted meanwhile is not counted and not an error."""
    await display.upload(own_file(setup_entry, "orphan.mp3"), "ALERT", b"x")
    _fail(display, "delete_media", WallDisplayError("gone"))
    assert await setup_entry.runtime_data.speaker.async_cleanup() == 0


def test_unknown_extension_becomes_mp3() -> None:
    """Unknown file types are stored as .mp3."""
    assert clip_filename("abc123", "a" * 64, "xyz").endswith(".mp3")
    assert clip_filename("abc123", "a" * 64, ".WAV").endswith(".wav")


async def test_browse_media(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Browsing shows media sources, filtered to audio."""
    with patch(
        "custom_components.wall_display_plus.media_player.media_source.async_browse_media",
        AsyncMock(return_value="tree"),
    ) as browse:
        entity = hass.data["entity_components"][MP_DOMAIN].get_entity(
            _eid(hass, MP_DOMAIN, "speaker")
        )
        assert await entity.async_browse_media() == "tree"
    content_filter = browse.call_args.kwargs["content_filter"]
    assert content_filter(MagicMock(media_content_type="audio/mpeg"))
    assert not content_filter(MagicMock(media_content_type="image/png"))


# -- switch, button, diagnostics --


async def test_switch_error(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A failing write is reported."""
    _fail(display, "get_motion_config", WallDisplayError("down"))
    with pytest.raises(HomeAssistantError, match="could not be changed"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: _eid(hass, SWITCH_DOMAIN, "motion_wakes_screen")},
            blocking=True,
        )


async def test_button_error(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A failing clear is reported."""
    _fail(display, "list_media", WallDisplayError("down"))
    with pytest.raises(HomeAssistantError, match="could not be deleted"):
        await hass.services.async_call(
            BUTTON_DOMAIN,
            SERVICE_PRESS,
            {ATTR_ENTITY_ID: _eid(hass, BUTTON_DOMAIN, "clear_cache")},
            blocking=True,
        )


async def test_diagnostics_display_unreachable(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Diagnostics work while the display is away."""
    display.reachable = False
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["clips_on_display"] == "unavailable: WallDisplayError"


async def test_stored_cache_is_loaded(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry, hass_storage
) -> None:
    """A cache written before is used after a restart."""
    hass_storage[f"wall_display_plus.{config_entry.entry_id}.cache"] = {
        "version": 1,
        "key": f"wall_display_plus.{config_entry.entry_id}.cache",
        "data": {
            "tag": "abcdef",
            "secret": "00" * 32,
            "entries": {
                "k": {"filename": "homeassistant_abcdef_k.mp3", "last_used": "2026-10-01T00:00:00"}
            },
        },
    }
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.runtime_data.speaker.entries == {
        "k": {"filename": "homeassistant_abcdef_k.mp3", "last_used": "2026-10-01T00:00:00"}
    }
