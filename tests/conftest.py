"""Fixtures: a wall display simulated in memory."""

from __future__ import annotations

from collections.abc import Generator
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


class FakeDisplay:
    """Behaves like the display's RPC interface, as far as the integration uses it."""

    def __init__(self) -> None:
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
        self.deleted: list[int] = []
        self.wake_screen = True
        self.motion_writes = 0
        self.clip_duration = 0.05
        self.fs_free: int | None = 20 * 1024**3
        self._next_id = 10
        self.client_password: str | None = None

    # -- helpers for tests --
    def by_name(self, filename: str) -> MediaItem | None:
        return next((i for i in self.library.values() if i.filename == filename), None)

    def clips(self) -> list[MediaItem]:
        return [
            i for i in self.library.values() if i.filename.startswith(("homeassistant_", "wdp_"))
        ]

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
            mac=MAC,
            model=self.model,
            firmware="2.8.2",
            app=self.app,
            auth_enabled=self.auth_enabled,
            fs_free=self.fs_free,
        )

    async def rpc(self, method: str, params: dict[str, Any] | None = None, **_: Any) -> dict:
        self._check_auth()
        if method == "Sys.GetConfig":
            return {"device": {"name": "Hallway Display"}}
        raise AssertionError(f"unexpected rpc {method}")

    async def list_media(self) -> list[MediaItem]:
        self._check_auth()
        return list(self.library.values())

    async def upload(self, filename: str, media_type: str, data: bytes) -> None:
        self._check_auth()
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
        return PlaybackResult(duration=self.clip_duration)

    async def stop(self) -> None:
        self._check_auth()

    async def get_media_status(self) -> dict[str, Any]:
        self._check_auth()
        return {"playback": {"enable": False, "media_type": "AUDIO"}}

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
