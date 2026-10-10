"""Polling of the wall display."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceInfo, WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import DOMAIN, UPDATE_INTERVAL
from .speaker import Speaker

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DisplayState:
    """What the integration knows about the display."""

    wake_screen: bool | None
    media_status: dict[str, Any]


@dataclass(slots=True)
class RuntimeData:
    """Objects shared by the platforms of one entry."""

    client: WallDisplayClient
    device: DeviceInfo
    coordinator: WallDisplayCoordinator
    speaker: Speaker


type WallDisplayConfigEntry = ConfigEntry[RuntimeData]


class WallDisplayCoordinator(DataUpdateCoordinator[DisplayState]):
    """Read the motion configuration and the media player status."""

    config_entry: WallDisplayConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WallDisplayConfigEntry,
        client: WallDisplayClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> DisplayState:
        try:
            motion = await self.client.get_motion_config()
            media = await self.client.get_media_status()
        except WallDisplayAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except WallDisplayError as err:
            raise UpdateFailed(str(err)) from err
        wake = motion.get("wake_screen")
        return DisplayState(
            wake_screen=wake if isinstance(wake, bool) else None,
            media_status=media,
        )
