"""Several displays playing together, and speaker groups (experimental)."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    SERVICE_MEDIA_STOP,
    STATE_IDLE,
    STATE_PLAYING,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus.audio import Announcement
from custom_components.wall_display_plus.const import (
    CONF_MEMBERS,
    CONF_TYPE,
    DOMAIN,
    TYPE_GROUP,
    UPDATE_INTERVAL,
)
from custom_components.wall_display_plus.diagnostics import async_get_config_entry_diagnostics
from custom_components.wall_display_plus.sync import get_sync_manager

from .conftest import FakeDisplay

# Test addresses (RFC 5737) and locally administered MAC addresses
DISPLAYS = {
    "192.0.2.11": "02a1b2c3d4e5",
    "192.0.2.12": "02a1b2c3d4e6",
    "192.0.2.13": "02a1b2c3d4e7",
}
TTS = "media-source://tts/tts.piper?message=Hallo&language=de_DE"
TTS_OTHER = "media-source://tts/tts.piper?message=Tsch%C3%BCss&language=de_DE"
# Starts closer together than this count as simultaneous in the simulation
TOGETHER = 0.05


@pytest.fixture
def displays() -> Generator[list[FakeDisplay]]:
    """Three simulated displays, picked by address."""
    fakes = {host: FakeDisplay(mac) for host, mac in DISPLAYS.items()}

    def factory(_session: Any, host: str, password: str | None) -> FakeDisplay:
        fakes[host].client_password = password
        return fakes[host]

    with (
        patch("custom_components.wall_display_plus.WallDisplayClient", side_effect=factory),
        patch(
            "custom_components.wall_display_plus.config_flow.WallDisplayClient",
            side_effect=factory,
        ),
        patch("custom_components.wall_display_plus.speaker.PLAYBACK_MARGIN", 0.0),
        patch("custom_components.wall_display_plus.speaker.DELETE_GRACE", 0.0),
    ):
        yield list(fakes.values())


@pytest.fixture
async def entries(hass: HomeAssistant, displays: list[FakeDisplay]) -> list[MockConfigEntry]:
    """The three displays, set up."""
    result = []
    for number, (host, mac) in enumerate(DISPLAYS.items(), start=1):
        entry = MockConfigEntry(
            domain=DOMAIN,
            title=f"Display {number}",
            unique_id=mac,
            data={CONF_HOST: host, CONF_PASSWORD: None},
        )
        entry.add_to_hass(hass)
        result.append(entry)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return result


@pytest.fixture
def tts_audio() -> Generator[AsyncMock]:
    """Let the TTS engine return a few bytes of 'mp3'."""
    with patch(
        "custom_components.wall_display_plus.audio.tts.async_get_media_source_audio",
        AsyncMock(return_value=("mp3", b"ID3fake-audio")),
    ) as mock:
        yield mock


def _speakers(hass: HomeAssistant) -> list[str]:
    registry = er.async_get(hass)
    ids = [
        registry.async_get_entity_id(MP_DOMAIN, DOMAIN, f"{mac}_speaker")
        for mac in DISPLAYS.values()
    ]
    assert all(ids)
    return [i for i in ids if i]


async def _play(
    hass: HomeAssistant,
    entity_ids: list[str],
    media_id: str = TTS,
    extra: dict[str, Any] | None = None,
    context: Context | None = None,
) -> None:
    data: dict[str, Any] = {
        ATTR_ENTITY_ID: entity_ids,
        ATTR_MEDIA_CONTENT_ID: media_id,
        ATTR_MEDIA_CONTENT_TYPE: "music",
    }
    if extra is not None:
        data[ATTR_MEDIA_EXTRA] = extra
    await hass.services.async_call(
        MP_DOMAIN, SERVICE_PLAY_MEDIA, data, blocking=True, context=context
    )


def _spread(displays: list[FakeDisplay]) -> float:
    starts = [d.played_at[-1] for d in displays]
    return max(starts) - min(starts)


# -- one action, several speakers --


async def test_speakers_of_one_call_play_together(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """Displays with the clip wait for the one that needs an upload."""
    speakers = _speakers(hass)
    await _play(hass, [speakers[0], speakers[2]])
    assert tts_audio.call_count == 1
    displays[1].upload_delay = 0.3

    started = time.monotonic()
    await _play(hass, speakers)

    assert tts_audio.call_count == 2  # once per call, not once per display
    assert [len(d.uploads) for d in displays] == [1, 1, 1]
    assert all(len(d.played) == n for d, n in zip(displays, (2, 1, 2), strict=True))
    assert _spread(displays) < TOGETHER
    assert min(d.played_at[-1] for d in displays) - started >= 0.3


async def test_each_display_keeps_its_own_cache_key(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """The shared audio is cached under each display's own secret key."""
    await _play(hass, _speakers(hass))
    names = {d.uploads[0][0] for d in displays}
    assert len(names) == 3


async def test_display_not_ready_in_time_does_not_play(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """The others start after the timeout; the late display fails and resets its volume."""
    displays[1].upload_delay = 0.4
    with (
        patch("custom_components.wall_display_plus.sync.SYNC_TIMEOUT", 0.1),
        pytest.raises(HomeAssistantError, match="not ready within 10 seconds"),
    ):
        await _play(hass, _speakers(hass), extra={"volume": 0.8})
    assert displays[0].played and displays[2].played
    assert not displays[1].played
    assert displays[1].volume_writes == [8, 5]


async def test_busy_display_misses_the_start(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """A display still playing a long clip does not hold back the others."""
    speakers = _speakers(hass)
    displays[1].clip_duration = 0.5
    await _play(hass, [speakers[1]], TTS_OTHER)
    displays[1].clip_duration = 0.05
    with (
        patch("custom_components.wall_display_plus.sync.SYNC_TIMEOUT", 0.1),
        pytest.raises(HomeAssistantError, match="not ready within"),
    ):
        await _play(hass, speakers)
    assert len(displays[1].played) == 1
    assert len(displays[1].uploads) == 1  # it did not even upload the clip
    assert displays[0].played and displays[2].played


async def test_failing_display_does_not_stop_the_others(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """An unreachable display fails at once; the others play without waiting."""
    displays[1].reachable = False
    with pytest.raises(HomeAssistantError, match="could not play"):
        await _play(hass, _speakers(hass))
    assert displays[0].played and displays[2].played
    assert _spread([displays[0], displays[2]]) < TOGETHER


async def test_audio_failure_fails_every_speaker(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """If the audio cannot be made, no display plays."""
    tts_audio.side_effect = RuntimeError("engine down")
    with pytest.raises(HomeAssistantError, match="RuntimeError"):
        await _play(hass, _speakers(hass))
    assert not any(d.played for d in displays)


async def test_calls_with_the_same_context_play_together(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """Separate calls of one action (like a media player group) are merged."""
    speakers = _speakers(hass)
    context = Context()
    await asyncio.gather(*(_play(hass, [entity_id], context=context) for entity_id in speakers))
    assert tts_audio.call_count == 1
    assert _spread(displays) < TOGETHER


async def test_different_media_are_not_merged(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """Other media or other extra in the same action play on their own."""
    speakers = _speakers(hass)
    context = Context()
    await asyncio.gather(
        _play(hass, [speakers[0]], TTS, context=context),
        _play(hass, [speakers[1]], TTS_OTHER, context=context),
        _play(hass, [speakers[2]], TTS, extra={"volume": 0.5}, context=context),
    )
    assert tts_audio.call_count == 3


async def test_without_context(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """Without a context every call plays on its own."""
    manager = get_sync_manager(hass)
    speakers = [manager.members[entity_id] for entity_id in _speakers(hass)]
    await asyncio.gather(*(manager.async_play(None, TTS, {}, None, [s]) for s in speakers))
    assert tts_audio.call_count == 3


async def test_announcement_is_shared(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """A URL is downloaded once for all displays."""
    with patch(
        "custom_components.wall_display_plus.sync.async_get_announcement",
        AsyncMock(return_value=Announcement("http://192.0.2.1/a.mp3", "mp3", b"ID3")),
    ) as fetch:
        await _play(hass, _speakers(hass), "http://192.0.2.1/a.mp3")
    assert fetch.call_count == 1
    assert all(d.played for d in displays)


# -- speaker groups --


async def _create_group(
    hass: HomeAssistant, members: list[str], name: str = "Downstairs"
) -> MockConfigEntry:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "group"}
    )
    assert result["step_id"] == "group"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: name, CONF_MEMBERS: members}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


def _group_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(MP_DOMAIN, DOMAIN, entry.entry_id)
    assert entity_id
    return entity_id


async def test_menu_offers_display_and_group(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """With two displays or more, the first step is a menu; display still works."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["menu_options"] == ["display", "group"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "display"}
    )
    assert result["step_id"] == "display"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.11"}
    )
    assert result["reason"] == "already_configured"


async def test_create_group(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """A group plays on its members together and shows them as attribute."""
    speakers = _speakers(hass)
    entry = await _create_group(hass, [*speakers[:2], speakers[0]])
    assert entry.data == {CONF_TYPE: TYPE_GROUP}
    assert entry.options == {CONF_MEMBERS: speakers[:2]}
    group = _group_id(hass, entry)
    assert group == "media_player.downstairs"
    state = hass.states.get(group)
    assert state.state == STATE_IDLE
    assert state.attributes[ATTR_ENTITY_ID] == speakers[:2]

    displays[0].clip_duration = displays[1].clip_duration = 30
    await _play(hass, [group])
    assert tts_audio.call_count == 1
    assert _spread(displays[:2]) < TOGETHER
    assert not displays[2].played
    await hass.async_block_till_done()
    assert hass.states.get(group).state == STATE_PLAYING


async def test_group_needs_two_speakers(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """A group needs at least two speakers."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "group"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Upstairs", CONF_MEMBERS: _speakers(hass)[:1]}
    )
    assert result["errors"] == {CONF_MEMBERS: "too_few_members"}


async def test_group_cannot_contain_a_group(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """Only speakers of displays are accepted as members, not other groups."""
    speakers = _speakers(hass)
    first = await _create_group(hass, speakers[:2])
    result = await hass.config_entries.options.async_init(first.entry_id)
    assert result["step_id"] == "members"
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MEMBERS: [_group_id(hass, first), speakers[2]]}
        )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MEMBERS: [speakers[2]]}
    )
    assert result["errors"] == {CONF_MEMBERS: "too_few_members"}


async def test_change_members(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """The members are changed in the options; the group reloads."""
    speakers = _speakers(hass)
    entry = await _create_group(hass, speakers[:2])
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MEMBERS: speakers[1:]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert hass.states.get(_group_id(hass, entry)).attributes[ATTR_ENTITY_ID] == speakers[1:]


async def test_speaker_in_group_and_named_directly_plays_once(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """A call to the group and to one of its members plays that member once."""
    speakers = _speakers(hass)
    entry = await _create_group(hass, speakers[:2])
    await _play(hass, [_group_id(hass, entry), speakers[0], speakers[2]])
    assert [len(d.played) for d in displays] == [1, 1, 1]
    assert tts_audio.call_count == 1
    assert _spread(displays) < TOGETHER


async def test_group_reports_failed_members(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """The group fails if a member fails, after the others have played."""
    entry = await _create_group(hass, _speakers(hass))
    displays[2].reachable = False
    with pytest.raises(HomeAssistantError, match="1 of 3 speakers could not play"):
        await _play(hass, [_group_id(hass, entry)])
    assert displays[0].played and displays[1].played


async def test_group_follows_member_availability(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """Unavailable members are left out; without any, the group is unavailable."""
    speakers = _speakers(hass)
    entry = await _create_group(hass, speakers[:2])
    group = _group_id(hass, entry)
    displays[0].reachable = False
    async_fire_time_changed(hass, dt_util.utcnow() + UPDATE_INTERVAL)
    await hass.async_block_till_done()
    assert hass.states.get(speakers[0]).state == STATE_UNAVAILABLE

    await _play(hass, [group])
    assert not displays[0].played and displays[1].played

    displays[1].reachable = False
    async_fire_time_changed(hass, dt_util.utcnow() + 2 * UPDATE_INTERVAL)
    await hass.async_block_till_done()
    assert hass.states.get(group).state == STATE_UNAVAILABLE


async def test_group_without_loaded_members(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """A group whose members are not speakers of this integration any more fails clearly."""
    entry = await _create_group(hass, _speakers(hass)[:2])
    get_sync_manager(hass).members.clear()
    with pytest.raises(HomeAssistantError, match="No speaker of the group is available"):
        await _play(hass, [_group_id(hass, entry)])


async def test_group_stop_and_browse(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """Stop goes to all members; browsing shows audio media sources."""
    entry = await _create_group(hass, _speakers(hass))
    group = _group_id(hass, entry)
    await hass.services.async_call(
        MP_DOMAIN, SERVICE_MEDIA_STOP, {ATTR_ENTITY_ID: group}, blocking=True
    )
    assert [d.stops for d in displays] == [1, 1, 1]

    entity = hass.data["entity_components"][MP_DOMAIN].get_entity(group)
    with patch(
        "custom_components.wall_display_plus.media_player.media_source.async_browse_media",
        AsyncMock(return_value="browsed"),
    ) as browse:
        assert await entity.async_browse_media() == "browsed"
    item = type("Item", (), {"media_content_type": "audio/mpeg"})()
    assert browse.call_args.kwargs["content_filter"](item)


async def test_group_stop_without_members(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """Stopping a group without available members does nothing."""
    entry = await _create_group(hass, _speakers(hass)[:2])
    entity = hass.data["entity_components"][MP_DOMAIN].get_entity(_group_id(hass, entry))
    for display in displays[:2]:
        display.reachable = False
    async_fire_time_changed(hass, dt_util.utcnow() + UPDATE_INTERVAL)
    await hass.async_block_till_done()
    await entity.async_media_stop()
    assert [d.stops for d in displays] == [0, 0, 0]


async def test_group_diagnostics_unload_and_remove(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """A group has short diagnostics, unloads and is removed without the displays."""
    entry = await _create_group(hass, _speakers(hass)[:2])
    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "type": "group",
        "members": 2,
    }
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert hass.states.get("media_player.downstairs").state == STATE_UNAVAILABLE
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert all(e.state is config_entries.ConfigEntryState.LOADED for e in entries)
    assert not any(d.deleted for d in displays)


async def test_unexpected_error_is_passed_on(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry], tts_audio
) -> None:
    """An error that is not the display's is raised as it is."""

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("bug")

    displays[0].find_media = broken  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="bug"):
        await _play(hass, _speakers(hass)[:1])


async def test_cancelled_session_releases_the_callers(
    hass: HomeAssistant, displays: list[FakeDisplay], entries: list[MockConfigEntry]
) -> None:
    """If the session is cancelled (shutdown), the callers do not wait forever."""
    manager = get_sync_manager(hass)
    speaker = manager.members[_speakers(hass)[0]]
    with (
        patch(
            "custom_components.wall_display_plus.sync.async_get_announcement",
            AsyncMock(side_effect=asyncio.CancelledError),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await manager.async_play(Context(), TTS, {}, None, [speaker])
