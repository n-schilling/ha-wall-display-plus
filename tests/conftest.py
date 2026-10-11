"""Fixtures: a wall display simulated in memory."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
import copy
import time
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from custom_components.wall_display_plus.api import (
    DeviceInfo,
    MediaItem,
    PlaybackResult,
    WallDisplayAuthError,
    WallDisplayError,
)
from custom_components.wall_display_plus.const import DOMAIN

MAC = "02a1b2c3d4e5"
HOST = "192.0.2.10"


def own_file(entry: MockConfigEntry, name: str) -> str:
    """Return a file name that belongs to the entry (our prefix and its tag)."""
    return f"homeassistant_{entry.runtime_data.speaker.tag}_{name}"


class FakeDisplay:
    """Behaves like the display's RPC interface, as far as the integration uses it."""

    def __init__(self, mac: str = MAC) -> None:
        self.mac = mac
        self.reachable = True
        self.password: str | None = None
        self.auth_enabled = False
        self.app = "WallDisplayV2"
        self.model = "SAWD-3A1XE10EU2"
        self.library: dict[int, MediaItem] = {
            1: MediaItem(1, "digital_alarm.mp3", "RINGTONE"),
            2: MediaItem(2, "song.mp3", "AUDIO"),
        }
        self.uploads: list[tuple[str, str, int]] = []
        self.played: list[int] = []
        self.played_at: list[float] = []
        self.upload_delay = 0.0
        self.stops = 0
        self.list_calls = 0
        self.cpu_calls = 0
        self.deleted: list[int] = []
        self.wake_screen = True
        self.motion_writes = 0
        self.clip_duration = 0.05
        # Firmware 2.8.2 reports no duration for alert clips
        self.reports_duration = True
        self.fs_free: int | None = 20 * 1024**3
        self.ram_free = 73 * 1024**2
        self.uptime = 10_000
        self.app_uptime = 9_990
        self.cpu_temperature: float | None = 44.4
        self.volume = 7
        self.volume_writes: list[int] = []
        self.app_restarts = 0
        self.ui_writes: list[dict[str, Any]] = []
        self.ui_config: dict[str, Any] = {
            "brightness": {
                "auto": True,
                "level": 19,
                "min_level": 25,
                "auto_off": {"enable": True, "by_lux": False, "timeout": 60},
                "auto_dim": {"enable": True, "timeout": 30},
            },
            "lock_type": "none",
            "screen_saver": {"enable": False, "timeout": 20, "priority_element": "CLOCK"},
        }
        self._next_id = 10
        self.client_password: str | None = None

    # -- helpers for tests --
    def by_name(self, filename: str) -> MediaItem | None:
        return next((i for i in self.library.values() if i.filename == filename), None)

    def clips(self) -> list[MediaItem]:
        return [i for i in self.library.values() if i.filename.startswith("homeassistant_")]

    def renumber(self, filename: str) -> int:
        """Give a file a new id, as after a factory reset and new upload."""
        item = self.by_name(filename)
        assert item
        del self.library[item.id]
        self._next_id += 1
        self.library[self._next_id] = MediaItem(self._next_id, filename, item.type, item.size)
        return self._next_id

    # -- client API --
    def _check(self) -> None:
        if not self.reachable:
            raise WallDisplayError("unreachable")

    def _check_auth(self) -> None:
        self._check()
        if self.auth_enabled and self.client_password != self.password:
            raise WallDisplayAuthError("The display rejected the password")

    async def get_device_info(self) -> DeviceInfo:
        self._check()
        return DeviceInfo(
            mac=self.mac,
            model=self.model,
            firmware="2.8.2",
            app=self.app,
            auth_enabled=self.auth_enabled,
            fs_free=self.fs_free,
            ram_free=self.ram_free,
            uptime=self.uptime,
            app_uptime=self.app_uptime,
        )

    async def rpc(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict:
        self._check_auth()
        if method == "Sys.GetConfig":
            return {"device": {"name": "Hallway Display"}}
        raise AssertionError(f"unexpected rpc {method}")

    async def list_media(self) -> list[MediaItem]:
        self._check_auth()
        self.list_calls += 1
        return list(self.library.values())

    async def upload(self, filename: str, media_type: str, data: bytes) -> None:
        self._check_auth()
        if self.upload_delay:
            await asyncio.sleep(self.upload_delay)
        existing = self.by_name(filename)
        if existing:
            del self.library[existing.id]
        self._next_id += 1
        self.library[self._next_id] = MediaItem(self._next_id, filename, media_type, len(data))
        self.uploads.append((filename, media_type, len(data)))
        if self.fs_free is not None:
            self.fs_free -= len(data)

    async def find_media(self, filename: str, attempts: int = 8) -> MediaItem | None:
        self._check_auth()
        return self.by_name(filename)

    async def delete_media(self, media_id: int) -> None:
        self._check_auth()
        if media_id not in self.library:
            raise WallDisplayError("no such file")
        if self.fs_free is not None:
            self.fs_free += self.library[media_id].size
        del self.library[media_id]
        self.deleted.append(media_id)

    async def play_alert(self, media_id: int) -> PlaybackResult:
        self._check_auth()
        assert self.library[media_id].type == "ALERT"
        self.played.append(media_id)
        self.played_at.append(time.monotonic())
        return PlaybackResult(duration=self.clip_duration if self.reports_duration else 0.0)

    async def stop(self) -> None:
        self._check_auth()
        self.stops += 1

    async def get_media_status(self) -> dict[str, Any]:
        self._check_auth()
        return {"playback": {"enable": False, "media_type": "AUDIO", "volume": self.volume}}

    async def set_volume(self, level: int) -> None:
        self._check_auth()
        self.volume = level
        self.volume_writes.append(level)

    async def get_ui_config(self) -> dict[str, Any]:
        self._check_auth()
        return copy.deepcopy(self.ui_config)

    async def set_ui_config(self, config: dict[str, Any]) -> None:
        self._check_auth()
        self.ui_writes.append(copy.deepcopy(config))
        self.ui_config.update(copy.deepcopy(config))

    async def get_cpu_temperature(self) -> float | None:
        self._check_auth()
        self.cpu_calls += 1
        return self.cpu_temperature

    async def restart_app(self) -> None:
        self._check_auth()
        self.app_restarts += 1

    async def get_motion_config(self) -> dict[str, Any]:
        self._check_auth()
        return {"enable": True, "wake_screen": self.wake_screen}

    async def set_wake_screen(self, enabled: bool) -> None:
        self._check_auth()
        self.wake_screen = enabled
        self.motion_writes += 1


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from custom_components."""


@pytest.fixture
def display() -> Generator[FakeDisplay]:
    """Replace the RPC client by the simulated display."""
    fake = FakeDisplay()

    def factory(_session: Any, _host: str, password: str | None) -> FakeDisplay:
        fake.client_password = password
        return fake

    with (
        patch("custom_components.wall_display_plus.WallDisplayClient", side_effect=factory),
        patch(
            "custom_components.wall_display_plus.config_flow.WallDisplayClient",
            side_effect=factory,
        ),
        patch("custom_components.wall_display_plus.speaker.PLAYBACK_MARGIN", 0.0),
        patch("custom_components.wall_display_plus.speaker.DELETE_GRACE", 0.0),
    ):
        yield fake


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured display."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Hallway Display",
        unique_id=MAC,
        data={CONF_HOST: HOST, CONF_PASSWORD: None},
    )


@pytest.fixture
async def setup_entry(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Set up the integration with the simulated display."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
