"""Polling of the wall display."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceInfo, WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import DOMAIN, UPDATE_INTERVAL
from .speaker import Speaker, owns

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DisplayState:
    """What the integration knows about the display."""

    wake_screen: bool | None
    media_status: dict[str, Any]
    device: DeviceInfo | None = None
    ui_config: dict[str, Any] = field(default_factory=dict)
    cpu_temperature: float | None = None
    cache_clips: int = 0
    cache_bytes: int = 0


@dataclass(slots=True)
class RuntimeData:
    """Objects shared by the platforms of one entry."""

    client: WallDisplayClient
    device: DeviceInfo
    coordinator: WallDisplayCoordinator
    speaker: Speaker


type WallDisplayConfigEntry = ConfigEntry[RuntimeData]


class WallDisplayCoordinator(DataUpdateCoordinator[DisplayState]):
    """Read the display's status and settings every 30 seconds."""

    config_entry: WallDisplayConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: WallDisplayConfigEntry,
        client: WallDisplayClient,
        speaker: Speaker,
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
        self.speaker = speaker

    async def _async_update_data(self) -> DisplayState:
        try:
            device = await self.client.get_device_info()
            motion = await self.client.get_motion_config()
            media = await self.client.get_media_status()
            ui_config = await self.client.get_ui_config()
            cpu_temperature = await self.client.get_cpu_temperature()
            ours = [i for i in await self.client.list_media() if owns(i.filename, self.speaker.tag)]
        except WallDisplayAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except WallDisplayError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        wake = motion.get("wake_screen")
        return DisplayState(
            wake_screen=wake if isinstance(wake, bool) else None,
            media_status=media,
            device=device,
            ui_config=ui_config,
            cpu_temperature=cpu_temperature,
            cache_clips=len(ours),
            cache_bytes=sum(i.size for i in ours),
        )

    async def async_set_ui(self, path: tuple[str, ...], value: Any) -> None:
        """Set one screen setting (path of at least two keys), only if it changes.

        Reads the current configuration first and sends the whole top-level
        section, as the display's web interface does. Every write makes the
        official integration for the display reload for a moment.
        """
        config = await self.client.get_ui_config()
        section = copy.deepcopy(config.get(path[0]))
        node = section = section if isinstance(section, dict) else {}
        for key in path[1:-1]:
            node = node.setdefault(key, {})
        if node.get(path[-1]) == value:
            return
        node[path[-1]] = value
        await self.client.set_ui_config({path[0]: section})
        config[path[0]] = section
        self.async_set_updated_data(_replace_ui(self.data, config))


def ui_value(state: DisplayState, path: tuple[str, ...]) -> Any:
    """Return a value of the screen configuration, None if missing."""
    node: Any = state.ui_config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _replace_ui(state: DisplayState, config: dict[str, Any]) -> DisplayState:
    new = copy.copy(state)
    new.ui_config = config
    return new
