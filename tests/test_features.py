"""Diagnostic sensors, app restart, volume per announcement, screen settings."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.media_player import (
    ATTR_MEDIA_CONTENT_ID,
    ATTR_MEDIA_CONTENT_TYPE,
    ATTR_MEDIA_EXTRA,
    DOMAIN as MP_DOMAIN,
    SERVICE_PLAY_MEDIA,
)
from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER_DOMAIN, SERVICE_SET_VALUE
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNKNOWN,
    EntityCategory,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.wall_display_plus.api import WallDisplayError
from custom_components.wall_display_plus.const import RETRY_INTERVAL, UPDATE_INTERVAL

from .conftest import MAC, FakeDisplay

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
    assert entity_id, key
    return entity_id


async def _enable(hass: HomeAssistant, entry: MockConfigEntry, domain: str, key: str) -> str:
    entity_id = _eid(hass, domain, key)
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    # Home Assistant reloads the entry 30 seconds after enabling an entity
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
    await hass.async_block_till_done()
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


POLL_SECONDS = int(UPDATE_INTERVAL.total_seconds()) + 1


async def _poll(hass: HomeAssistant, times: int = 1) -> None:
    """Let the regular poll run (times rounds)."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=POLL_SECONDS * times))
    await hass.async_block_till_done()


# -- diagnostic sensors --


async def test_device_sensors(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Memory and storage come from the device, as diagnostic entities."""
    memory = hass.states.get(_eid(hass, "sensor", "memory_free"))
    assert float(memory.state) == pytest.approx(76.5, abs=0.1)  # 73 MiB in MB
    assert memory.attributes["unit_of_measurement"] == "MB"
    storage = hass.states.get(_eid(hass, "sensor", "storage_free"))
    assert float(storage.state) == pytest.approx(21.5, abs=0.1)  # 20 GiB in GB
    entry = er.async_get(hass).async_get(_eid(hass, "sensor", "memory_free"))
    assert entry.entity_category is EntityCategory.DIAGNOSTIC


async def test_cache_sensor_shows_file_prefix(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The cached clips sensor names the prefix of this entry's files, nothing else."""
    state = hass.states.get(_eid(hass, "sensor", "cache_clips"))
    tag = setup_entry.runtime_data.speaker.tag
    assert state.attributes["file_prefix"] == f"homeassistant_{tag}_"
    other = hass.states.get(_eid(hass, "sensor", "cache_size"))
    assert "file_prefix" not in other.attributes


async def test_rarely_used_sensors_are_disabled(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """CPU temperature and hit rate are disabled by default."""
    registry = er.async_get(hass)
    for key in ("cpu_temperature", "cache_hit_rate"):
        assert registry.async_get(_eid(hass, "sensor", key)).disabled_by is not None


async def test_cpu_temperature(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The CPU temperature shows once enabled."""
    entity_id = await _enable(hass, setup_entry, "sensor", "cpu_temperature")
    assert float(hass.states.get(entity_id).state) == pytest.approx(44.4)


async def test_speaker_sensors(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Cache, delay and hit rate follow the announcements."""
    hit_rate = await _enable(hass, setup_entry, "sensor", "cache_hit_rate")
    assert hass.states.get(_eid(hass, "sensor", "announcement_delay")).state == STATE_UNKNOWN
    assert hass.states.get(hit_rate).state == STATE_UNKNOWN

    await _play(hass)
    await _play(hass)
    await _poll(hass)

    assert hass.states.get(_eid(hass, "sensor", "cache_clips")).state == "1"
    assert float(hass.states.get(_eid(hass, "sensor", "cache_size")).state) == pytest.approx(
        0.0, abs=0.01
    )
    delay = hass.states.get(_eid(hass, "sensor", "announcement_delay"))
    assert float(delay.state) >= 0
    assert hass.states.get(hit_rate).state == "50.0"


async def test_app_started_is_stable_and_shows_restarts(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, freezer
) -> None:
    """The app start does not jitter with the uptime, but moves on an app restart."""
    entity_id = _eid(hass, "sensor", "app_started")
    first = hass.states.get(entity_id).state
    freezer.tick(timedelta(seconds=POLL_SECONDS))
    display.app_uptime += POLL_SECONDS + 1  # time passes, with a second of jitter
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == first
    freezer.tick(timedelta(seconds=POLL_SECONDS))
    display.app_uptime = 5  # the app restarted
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state != first


async def test_sensors_without_device_values(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A firmware that reports no memory or uptime leaves the sensors unknown."""
    display.ram_free = None  # type: ignore[assignment]
    display.app_uptime = None  # type: ignore[assignment]
    await _poll(hass)
    assert hass.states.get(_eid(hass, "sensor", "memory_free")).state == STATE_UNKNOWN
    assert hass.states.get(_eid(hass, "sensor", "app_started")).state == STATE_UNKNOWN


# -- restart the app --


async def test_restart_app(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The button restarts the display app."""
    entity_id = _eid(hass, BUTTON_DOMAIN, "restart_app")
    await hass.services.async_call(
        BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert display.app_restarts == 1

    async def fail() -> None:
        raise WallDisplayError("down")

    display.restart_app = fail  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError, match="could not be restarted"):
        await hass.services.async_call(
            BUTTON_DOMAIN, SERVICE_PRESS, {ATTR_ENTITY_ID: entity_id}, blocking=True
        )


# -- volume per announcement --


async def test_volume_per_announcement_is_restored(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """extra.volume sets the volume for the clip and goes back afterwards."""
    await _play(hass, {"volume": 0.8})
    assert display.volume_writes == [8]
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.volume_writes == [8, 7]
    assert display.volume == 7


async def test_same_volume_writes_nothing(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """The current volume is not written again."""
    await _play(hass, {"volume": 0.7})
    await _play(hass)  # the announcement volume, 70 %
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.volume_writes == []


async def test_next_announcement_restores_first(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """An announcement without volume does not play at the previous one's volume."""
    display.clip_duration = 30  # the restore would come much later
    await _play(hass, {"volume": 1.0})
    with patch("custom_components.wall_display_plus.speaker.asyncio.sleep", AsyncMock()):
        await _play(hass)
    assert display.volume_writes[:2] == [10, 7]
    assert display.volume == 7


async def test_announcement_volume(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Announcements without their own volume play at the announcement volume.

    The display's volume (music) is set back afterwards.
    """
    entity_id = _eid(hass, NUMBER_DOMAIN, "announcement_volume")
    assert hass.states.get(entity_id).state == "70.0"
    assert er.async_get(hass).async_get(entity_id).entity_category is EntityCategory.CONFIG
    display.volume = 3
    await _play(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.volume_writes == [7, 3]

    await hass.services.async_call(
        NUMBER_DOMAIN, SERVICE_SET_VALUE, {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: 40}, blocking=True
    )
    assert hass.states.get(entity_id).state == "40.0"
    await _play(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.volume_writes == [7, 3, 4, 3]
    await _play(hass, {"volume": 0.9})  # its own volume wins
    await hass.async_block_till_done(wait_background_tasks=True)
    assert display.volume_writes[-2:] == [9, 3]


async def test_volume_zero_plays_nothing(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """At 0 % nothing runs: no text to speech, no upload, no volume change.

    An announcement with its own volume (an alarm) still plays.
    """
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: _eid(hass, NUMBER_DOMAIN, "announcement_volume"), ATTR_VALUE: 0},
        blocking=True,
    )
    await _play(hass)
    assert tts_audio.call_count == 0
    assert display.uploads == []
    assert display.played == []
    assert display.volume_writes == []

    await _play(hass, {"volume": 1.0})
    assert len(display.played) == 1
    assert display.volume_writes == [10]


async def test_own_volume_zero_plays_nothing(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """extra.volume 0 skips the announcement even at a normal announcement volume."""
    await _play(hass, {"volume": 0})
    assert tts_audio.call_count == 0
    assert display.played == []


async def test_announcement_volume_survives_restart(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry, tts_audio
) -> None:
    """The announcement volume set before a restart is kept."""
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("number.hallway_display_announcement_volume", "30.0"),
                {
                    "native_value": 30.0,
                    "native_min_value": 0,
                    "native_max_value": 100,
                    "native_step": 10,
                    "native_unit_of_measurement": "%",
                },
            )
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.runtime_data.speaker.announcement_volume == 3
    await _play(hass)
    assert display.volume_writes == [3]


@pytest.mark.parametrize("volume", [1.5, -0.1, "loud", True])
async def test_invalid_volume(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, volume: Any
) -> None:
    """Invalid volumes are refused before anything happens."""
    with pytest.raises(ServiceValidationError, match="volume"):
        await _play(hass, {"volume": volume})
    assert display.uploads == []


async def test_volume_restore_error_is_ignored(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """If going back fails, the next announcement still works."""
    await _play(hass, {"volume": 0.9})

    async def fail(level: int) -> None:
        raise WallDisplayError("down")

    display.set_volume = fail  # type: ignore[method-assign]
    await hass.async_block_till_done(wait_background_tasks=True)
    await _play(hass)
    assert len(display.played) == 2


# -- screen settings --


async def test_screen_switches_follow_the_display(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The switches show the screen configuration."""
    expected = {
        "dim_screen": STATE_ON,
        "turn_screen_off": STATE_ON,
        "screen_saver": STATE_OFF,
    }
    for key, state in expected.items():
        assert hass.states.get(_eid(hass, SWITCH_DOMAIN, key)).state == state


async def test_screen_switch_writes_whole_section(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A change sends the whole brightness section, other values unchanged."""
    entity_id = _eid(hass, SWITCH_DOMAIN, "dim_screen")
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert display.ui_writes == [
        {
            "brightness": {
                "auto": True,
                "level": 19,
                "min_level": 25,
                "auto_off": {"enable": True, "by_lux": False, "timeout": 60},
                "auto_dim": {"enable": False, "timeout": 30},
            }
        }
    ]
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_screen_setting_writes_only_on_change(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Setting the current value writes nothing (every write reloads the official integration)."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: _eid(hass, SWITCH_DOMAIN, "dim_screen")},
        blocking=True,
    )
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: _eid(hass, NUMBER_DOMAIN, "dim_after"), ATTR_VALUE: 30},
        blocking=True,
    )
    assert display.ui_writes == []


@pytest.mark.parametrize(
    ("key", "value", "path"),
    [
        ("dim_after", 15, ("brightness", "auto_dim", "timeout")),
        ("screen_off_after", 120, ("brightness", "auto_off", "timeout")),
        ("screen_saver_after", 300, ("screen_saver", "timeout")),
        ("minimum_brightness", 10, ("brightness", "min_level")),
    ],
)
async def test_screen_numbers(
    hass: HomeAssistant,
    display: FakeDisplay,
    setup_entry: MockConfigEntry,
    key: str,
    value: int,
    path: tuple[str, ...],
) -> None:
    """Each number writes its value to the right place."""
    entity_id = _eid(hass, NUMBER_DOMAIN, key)
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
        blocking=True,
    )
    node: Any = display.ui_config
    for part in path:
        node = node[part]
    assert node == value
    assert float(hass.states.get(entity_id).state) == value
    assert display.ui_config["lock_type"] == "none"  # other sections untouched


async def test_timeouts_are_limited(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Timeouts stay within 10-3600 seconds."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: _eid(hass, NUMBER_DOMAIN, "dim_after"), ATTR_VALUE: 5},
            blocking=True,
        )


async def test_screen_setting_errors(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A failing write is reported for switches and numbers."""

    async def fail(config: dict[str, Any]) -> None:
        raise WallDisplayError("down")

    display.set_ui_config = fail  # type: ignore[method-assign]
    with pytest.raises(HomeAssistantError, match="could not be changed"):
        await hass.services.async_call(
            SWITCH_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: _eid(hass, SWITCH_DOMAIN, "screen_saver")},
            blocking=True,
        )
    with pytest.raises(HomeAssistantError, match="could not be changed"):
        await hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: _eid(hass, NUMBER_DOMAIN, "minimum_brightness"), ATTR_VALUE: 50},
            blocking=True,
        )


async def test_no_manual_brightness_entities(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Manual and automatic brightness are not offered (could leave the screen black)."""
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(NUMBER_DOMAIN, "wall_display_plus", f"{MAC}_brightness")
        is None
    )
    assert (
        registry.async_get_entity_id(SWITCH_DOMAIN, "wall_display_plus", f"{MAC}_auto_brightness")
        is None
    )


async def test_missing_screen_values(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A firmware without a screen saver section leaves its entities unknown."""
    del display.ui_config["screen_saver"]
    await _poll(hass)
    assert hass.states.get(_eid(hass, SWITCH_DOMAIN, "screen_saver")).state == STATE_UNKNOWN
    assert hass.states.get(_eid(hass, NUMBER_DOMAIN, "screen_saver_after")).state == STATE_UNKNOWN
    # Writing creates the section again
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: _eid(hass, SWITCH_DOMAIN, "screen_saver")},
        blocking=True,
    )
    assert display.ui_config["screen_saver"] == {"enable": True}


# -- polling --


async def test_polls_every_ten_minutes(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The display is weak; it is polled rarely."""
    assert setup_entry.runtime_data.coordinator.update_interval == timedelta(minutes=10)


async def test_upload_refreshes_at_once(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """After an upload the cache sensors are right without waiting for the poll."""
    entity_id = _eid(hass, "sensor", "cache_clips")
    assert hass.states.get(entity_id).state == "0"
    await _play(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "1"


async def test_cache_hit_does_not_poll(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """Replaying a cached clip changes nothing in the library and asks the display nothing more."""
    await _play(hass)
    await hass.async_block_till_done()
    calls = 0
    original = display.get_device_info

    async def counting() -> Any:
        nonlocal calls
        calls += 1
        return await original()

    display.get_device_info = counting  # type: ignore[method-assign]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))  # past the cooldown
    await hass.async_block_till_done()
    await _play(hass)
    await hass.async_block_till_done()
    assert calls == 0


async def test_failed_announcement_refreshes_at_once(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry, tts_audio
) -> None:
    """A failed announcement polls at once; that poll counts as the first failed one."""
    display.reachable = False
    with pytest.raises(HomeAssistantError):
        await _play(hass)
    await hass.async_block_till_done()
    assert setup_entry.runtime_data.coordinator.update_interval == RETRY_INTERVAL
    assert hass.states.get(_eid(hass, SWITCH_DOMAIN, "dim_screen")).state == "on"
