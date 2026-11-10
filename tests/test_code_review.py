"""Fixes from the code and security review of 0.1.9."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import struct
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER_DOMAIN, SERVICE_SET_VALUE
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_PASSWORD,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus import async_remove_entry
from custom_components.wall_display_plus.api import (
    MediaItem,
    WallDisplayAuthError,
    WallDisplayClient,
    WallDisplayError,
    WallDisplayUnsupportedError,
)
from custom_components.wall_display_plus.audio import audio_duration
from custom_components.wall_display_plus.const import DOMAIN, UPDATE_INTERVAL
from custom_components.wall_display_plus.coordinator import device_issue_id

from .conftest import HOST, MAC, FakeDisplay

TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"
PASSWORD = "secret"


@pytest.fixture
def tts_audio():
    """Let the TTS engine return a few bytes of 'mp3'."""
    with patch(
        "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", b"ID3fake-audio")),
    ) as mock:
        yield mock


def _eid(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{MAC}_{key}")
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


async def _poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    freezer.tick(UPDATE_INTERVAL + timedelta(seconds=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def _fail(display: FakeDisplay, method: str, error: Exception) -> None:
    async def failing(*args: Any, **kwargs: Any) -> Any:
        raise error

    setattr(display, method, failing)


# -- screen settings are written one at a time --


async def test_screen_settings_written_at_the_same_time_keep_both(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Two settings of one section, changed together, both reach the display."""
    original = display.get_ui_config

    async def slow() -> dict[str, Any]:
        config = await original()
        await asyncio.sleep(0.01)  # the other write would read the same old section
        return config

    display.get_ui_config = slow  # type: ignore[method-assign]
    display.ui_config["brightness"]["auto_dim"]["enable"] = False
    await asyncio.gather(
        hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: _eid(hass, SWITCH_DOMAIN, "dim_screen")},
            blocking=True,
        ),
        hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: _eid(hass, NUMBER_DOMAIN, "dim_after"), ATTR_VALUE: 45},
            blocking=True,
        ),
    )
    assert display.ui_config["brightness"]["auto_dim"] == {"enable": True, "timeout": 45}


# -- the device at the address is checked at every poll --


async def test_other_device_at_the_address_gets_nothing(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After an address change, nothing is read from or sent to another display."""
    speaker = _eid(hass, MP_DOMAIN, "speaker")
    await hass.async_block_till_done()
    display.mac = "aabbccddeeff"
    lists = display.list_calls
    await _poll(hass, freezer)
    # Unavailable at once, not after several failed polls
    assert hass.states.get(speaker).state == STATE_UNAVAILABLE
    assert display.list_calls == lists
    issue = ir.async_get(hass).async_get_issue(DOMAIN, device_issue_id(setup_entry))
    assert issue is not None
    assert issue.translation_key == "wrong_device"
    assert issue.translation_placeholders == {
        "title": "Hallway Display",
        "host": HOST,
        "mac": "aabbccddeeff",
    }
    # Back at its address: available again, the issue is gone
    display.mac = MAC
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(speaker).state != STATE_UNAVAILABLE
    assert ir.async_get(hass).async_get_issue(DOMAIN, device_issue_id(setup_entry)) is None


@pytest.fixture
def password_entry(display: FakeDisplay) -> MockConfigEntry:
    """A display with a password."""
    display.auth_enabled = True
    display.password = PASSWORD
    return MockConfigEntry(
        domain=DOMAIN,
        title="Hallway Display",
        unique_id=MAC,
        data={CONF_HOST: HOST, CONF_PASSWORD: PASSWORD},
    )


async def test_setup_refuses_a_device_without_the_password(
    hass: HomeAssistant, display: FakeDisplay, password_entry: MockConfigEntry
) -> None:
    """With a stored password, a device that asks for none gets nothing."""
    display.auth_enabled = False
    password_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(password_entry.entry_id)
    assert password_entry.state is ConfigEntryState.SETUP_RETRY
    assert "no longer asks for the password" in str(password_entry.reason)
    assert display.list_calls == 0
    issue = ir.async_get(hass).async_get_issue(DOMAIN, device_issue_id(password_entry))
    assert issue is not None
    assert issue.translation_key == "auth_disabled"


async def test_poll_refuses_a_device_without_the_password(
    hass: HomeAssistant,
    display: FakeDisplay,
    password_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    tts_audio,
) -> None:
    """A display that stops asking for its password is unavailable until it asks again."""
    password_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(password_entry.entry_id)
    await hass.async_block_till_done()
    speaker = _eid(hass, MP_DOMAIN, "speaker")
    display.auth_enabled = False
    lists = display.list_calls
    await _poll(hass, freezer)
    assert hass.states.get(speaker).state == STATE_UNAVAILABLE
    assert display.list_calls == lists
    assert ir.async_get(hass).async_get_issue(DOMAIN, device_issue_id(password_entry))
    display.auth_enabled = True
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(speaker).state != STATE_UNAVAILABLE
    assert not ir.async_get(hass).async_get_issue(DOMAIN, device_issue_id(password_entry))


async def test_removal_deletes_nothing_on_another_device(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Removing the entry deletes clips only on the configured display."""
    await _play(hass)
    assert display.clips()
    display.mac = "aabbccddeeff"
    await async_remove_entry(hass, setup_entry)
    assert display.clips()
    assert not display.deleted


# -- volume after an announcement --


async def test_failed_volume_restore_is_tried_again(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """If going back to the music volume fails, it is kept and tried once more."""
    display.volume = 3
    original = display.set_volume
    attempts: list[int] = []

    async def flaky(level: int) -> None:
        attempts.append(level)
        if len(attempts) == 2:  # the first try to go back to 3
            raise WallDisplayError("timeout")
        await original(level)

    display.set_volume = flaky  # type: ignore[method-assign]
    with patch("custom_components.wall_display_plus.speaker.RESTORE_RETRY_DELAY", 0.01):
        await _play(hass)
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
    assert attempts == [7, 3, 3]
    assert display.volume == 3


async def test_announcement_volume_never_becomes_the_music_volume(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Even if both tries fail, the next announcement goes back to the music volume."""
    display.volume = 3
    original = display.set_volume
    attempts: list[int] = []

    async def flaky(level: int) -> None:
        attempts.append(level)
        if len(attempts) in (2, 3):  # going back fails twice
            raise WallDisplayError("timeout")
        await original(level)

    display.set_volume = flaky  # type: ignore[method-assign]
    with patch("custom_components.wall_display_plus.speaker.RESTORE_RETRY_DELAY", 0.01):
        await _play(hass)
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
        assert display.volume == 7
        await _play(hass)
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()
    # The next turn goes back to 3 first; the clip plays at 7, then back to 3
    assert attempts[3:] == [3, 7, 3]
    assert display.volume == 3


# -- a rejected password starts re-authentication everywhere --


@pytest.mark.parametrize(
    ("domain", "service", "key", "data", "method"),
    [
        (SWITCH_DOMAIN, SERVICE_TURN_ON, "dim_screen", {}, "set_ui_config"),
        (SWITCH_DOMAIN, SERVICE_TURN_ON, "motion_wakes_screen", {}, "get_motion_config"),
        (NUMBER_DOMAIN, SERVICE_SET_VALUE, "dim_after", {ATTR_VALUE: 45}, "set_ui_config"),
        (BUTTON_DOMAIN, SERVICE_PRESS, "clear_cache", {}, "list_media"),
        (BUTTON_DOMAIN, SERVICE_PRESS, "restart_app", {}, "restart_app"),
    ],
)
async def test_rejected_password_starts_reauth(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    domain: str,
    service: str,
    key: str,
    data: dict[str, Any],
    method: str,
) -> None:
    """Switches, numbers and buttons ask for the new password at once."""
    display.ui_config["brightness"]["auto_dim"]["enable"] = False
    display.wake_screen = False
    _fail(display, method, WallDisplayAuthError("The display rejected the password"))
    with pytest.raises(HomeAssistantError, match="rejected the password"):
        await hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: _eid(hass, domain, key), **data}, blocking=True
        )
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


# -- unexpected answers of the display --


async def test_unreadable_library_entries_are_left_out(hass: HomeAssistant) -> None:
    """Entries of another shape are skipped instead of failing the poll."""
    client = WallDisplayClient(MagicMock(), HOST, None)
    client.rpc = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "list": [
                {"id": "x", "filename": "a.mp3"},
                {"id": True, "filename": "b.mp3"},
                {"id": 3, "filename": None},
                None,
                {"id": 4, "filename": "d.mp3", "size": "12k"},
                {"id": 5, "filename": "e.mp3", "size": 120, "type": "ALERT"},
            ]
        }
    )
    assert await client.list_media() == [
        MediaItem(4, "d.mp3", "", 0),
        MediaItem(5, "e.mp3", "ALERT", 120),
    ]
    client.rpc = AsyncMock(return_value={"list": {"id": 1}})  # type: ignore[method-assign]
    assert await client.list_media() == []


@pytest.mark.parametrize(
    ("result", "duration"),
    [
        ({"audio_clip": None, "playback": []}, 0.0),
        ({"audio_clip": {"media_meta": []}}, 0.0),
        ({"audio_clip": {"media_meta": {"duration": "1500"}}}, 0.0),
        ({"audio_clip": {"media_meta": {"duration": True}}}, 0.0),
        ({"playback": {"media_meta": {"duration": 1500}}}, 1.5),
    ],
)
async def test_unexpected_play_answer(
    hass: HomeAssistant, result: dict[str, Any], duration: float
) -> None:
    """A duration in an unexpected shape counts as unknown."""
    client = WallDisplayClient(MagicMock(), HOST, None)
    client.rpc = AsyncMock(return_value=result)  # type: ignore[method-assign]
    assert (await client.play_alert(1)).duration == duration


def test_damaged_audio_gets_an_estimated_length() -> None:
    """Any error while reading the length falls back to the estimate."""
    with patch("custom_components.wall_display_plus.audio.mutagen.File", side_effect=struct.error):
        assert audio_duration(b"x" * 4000) == 1.0


# -- firmware updates --


async def test_firmware_update_shows_without_reload(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    tts_audio,
) -> None:
    """The device page and the repair issue name the firmware of the last poll."""
    display.firmware = "2.9.0"
    await setup_entry.runtime_data.coordinator.async_refresh()
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, MAC), setup_entry.entry_id)
    assert device is not None
    assert device.sw_version == "2.9.0"
    _fail(display, "play_alert", WallDisplayUnsupportedError("PlayAlert: -114"))
    with pytest.raises(HomeAssistantError):
        await _play(hass)
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"unsupported_firmware_{setup_entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_placeholders["firmware"] == "2.9.0"


# -- adding a known display by hand --


async def test_adding_a_known_display_stores_the_new_password(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """The password checked against the display is kept, not only the address."""
    config_entry.add_to_hass(hass)
    display.auth_enabled = True
    display.password = PASSWORD
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.20", CONF_PASSWORD: PASSWORD}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data == {CONF_HOST: "192.0.2.20", CONF_PASSWORD: PASSWORD}


# -- library changes update the cache sensors without a full poll --


async def test_cache_statistics_follow_deletions(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Clearing the cache updates the sensors after the cooldown, with one request."""
    await _play(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=6))
    await hass.async_block_till_done()
    clips = _eid(hass, "sensor", "cache_clips")
    assert hass.states.get(clips).state == "1"
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _eid(hass, BUTTON_DOMAIN, "clear_cache")},
        blocking=True,
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=12))
    await hass.async_block_till_done()
    assert hass.states.get(clips).state == "0"


async def test_cache_statistics_skip_while_unavailable(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Without a working poll, the cache statistics wait for the next one."""
    coordinator = setup_entry.runtime_data.coordinator
    coordinator.last_update_success = False
    lists = display.list_calls
    await coordinator._async_refresh_cache_stats()
    assert display.list_calls == lists
    coordinator.last_update_success = True
    _fail(display, "list_media", WallDisplayError("timeout"))
    await coordinator._async_refresh_cache_stats()  # only logged
    assert coordinator.data.cache_clips == 0
