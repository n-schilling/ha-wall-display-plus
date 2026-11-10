"""Polling of the wall display."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field, replace
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceInfo, WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import (
    CONF_TYPE,
    DOMAIN,
    RETRY_INTERVAL,
    TYPE_GROUP,
    UNAVAILABLE_AFTER_FAILURES,
    UPDATE_INTERVAL,
)
from .speaker import Speaker, owns

_LOGGER = logging.getLogger(__name__)

# Seconds to wait after a clip was uploaded or deleted before the cache
# sensors are read again; several changes in a row cost one request.
CACHE_STATS_COOLDOWN = 5.0


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


def is_group(entry: ConfigEntry[Any]) -> bool:
    """Return whether the entry is a speaker group instead of a display."""
    return entry.data.get(CONF_TYPE) == TYPE_GROUP


class DeviceChangedError(Exception):
    """Another device, or one without the password, answers at the address."""

    def __init__(self, key: str, placeholders: dict[str, str]) -> None:
        """Remember the translation key and its placeholders."""
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


def check_device(entry: ConfigEntry[Any], device: DeviceInfo) -> None:
    """Raise DeviceChangedError unless the device at the address is the configured one.

    Checked before anything is sent: a device with another MAC is a different
    display, and a device that no longer asks for the stored password may be
    another device that took over the address.
    """
    host = str(entry.data[CONF_HOST])
    if entry.unique_id and device.mac != entry.unique_id:
        raise DeviceChangedError("wrong_device", {"host": host, "mac": device.mac})
    if entry.data.get(CONF_PASSWORD) and not device.auth_enabled:
        raise DeviceChangedError("auth_disabled", {"host": host})


def device_issue_id(entry: ConfigEntry[Any]) -> str:
    """Return the id of the repair issue for a changed device at the address."""
    return f"device_changed_{entry.entry_id}"


@callback
def async_create_device_issue(
    hass: HomeAssistant, entry: ConfigEntry[Any], err: DeviceChangedError
) -> None:
    """Tell the user that nothing is sent to the device at the address."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        device_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=err.key,
        translation_placeholders={"title": entry.title, **err.placeholders},
    )


class WallDisplayCoordinator(DataUpdateCoordinator[DisplayState]):
    """Read the display's status and settings every 10 minutes.

    A single failed poll keeps the last values and is repeated after a minute;
    only several failed polls in a row make the entities unavailable.
    """

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
        self._failures = 0
        # Screen settings are written as whole sections: one write at a time
        self._ui_lock = asyncio.Lock()
        self._cache_stats = Debouncer(
            hass,
            _LOGGER,
            cooldown=CACHE_STATS_COOLDOWN,
            immediate=False,
            function=self._async_refresh_cache_stats,
            background=True,
        )

    async def async_shutdown(self) -> None:
        """Stop the pending cache statistics as well."""
        await super().async_shutdown()
        self._cache_stats.async_shutdown()

    async def _async_update_data(self) -> DisplayState:
        try:
            state = await self._async_read()
        except DeviceChangedError as err:
            # Not a hiccup: the last values would be those of another device
            self.update_interval = RETRY_INTERVAL
            async_create_device_issue(self.hass, self.config_entry, err)
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key=err.key,
                translation_placeholders=err.placeholders,
            ) from err
        except WallDisplayError as err:
            self._failures += 1
            self.update_interval = RETRY_INTERVAL
            if self.data is None or self._failures >= UNAVAILABLE_AFTER_FAILURES:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="update_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            _LOGGER.debug("Poll %s failed, keeping the last values: %s", self._failures, err)
            return self.data
        self._failures = 0
        self.update_interval = UPDATE_INTERVAL
        ir.async_delete_issue(self.hass, DOMAIN, device_issue_id(self.config_entry))
        self._update_firmware(state.device)
        return state

    @callback
    def _update_firmware(self, device: DeviceInfo | None) -> None:
        """Show a firmware update on the device page without a reload."""
        if device is None or not device.firmware:
            return
        registry = dr.async_get(self.hass)
        entry = registry.async_get_device_by_identifier(
            (DOMAIN, device.mac), self.config_entry.entry_id
        )
        if entry is not None and entry.sw_version != device.firmware:
            registry.async_update_device(entry.id, sw_version=device.firmware)

    async def _async_read(self) -> DisplayState:
        try:
            device = await self.client.get_device_info()
            check_device(self.config_entry, device)
            motion = await self.client.get_motion_config()
            media = await self.client.get_media_status()
            ui_config = await self.client.get_ui_config()
            ours = [i for i in await self.client.list_media() if owns(i.filename, self.speaker.tag)]
            cpu_temperature = await self._async_cpu_temperature()
        except WallDisplayAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
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

    async def _async_cpu_temperature(self) -> float | None:
        """Read the CPU temperature, only for an enabled sensor; failing is no error.

        The sensor is disabled by default, and the method is missing on some
        firmware: neither may cost a request or make the display unavailable.
        """
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{self.config_entry.unique_id}_cpu_temperature"
        )
        if entity_id is None or (entry := registry.async_get(entity_id)) is None or entry.disabled:
            return None
        try:
            return await self.client.get_cpu_temperature()
        except WallDisplayAuthError:
            raise
        except WallDisplayError as err:
            _LOGGER.debug("CPU temperature not available: %s", err)
            return None

    async def async_set_ui(self, path: tuple[str, ...], value: Any) -> None:
        """Set one screen setting (path of at least two keys), only if it changes.

        Reads the current configuration first and sends the whole top-level
        section, as the display's web interface does. Every write makes the
        official integration for the display reload for a moment.
        """
        async with self._ui_lock:
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

    async def async_request_cache_stats(self) -> None:
        """Read the cache sensors again soon, without a full poll."""
        await self._cache_stats.async_call()

    async def _async_refresh_cache_stats(self) -> None:
        """Count this integration's clips on the display (one request)."""
        if self.data is None or not self.last_update_success:
            return
        try:
            items = await self.client.list_media()
        except WallDisplayError as err:
            _LOGGER.debug("Cache statistics not updated: %s", err)
            return
        ours = [i for i in items if owns(i.filename, self.speaker.tag)]
        self.async_set_updated_data(
            replace(self.data, cache_clips=len(ours), cache_bytes=sum(i.size for i in ours))
        )


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
