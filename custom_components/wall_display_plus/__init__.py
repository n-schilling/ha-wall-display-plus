"""Wall Display Plus: speaker and motion settings for a wall display."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
from typing import Any

from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .api import WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import (
    CLEANUP_INTERVAL,
    CONF_CACHE_DAYS,
    CONF_CACHE_MAX_MB,
    DEFAULT_CACHE_DAYS,
    DEFAULT_CACHE_MAX_MB,
    DOMAIN,
    STORAGE_VERSION,
)
from .coordinator import RuntimeData, WallDisplayConfigEntry, WallDisplayCoordinator
from .speaker import Speaker, owns

_LOGGER = logging.getLogger(__name__)

# Seconds to delete the clips when the integration is removed.
REMOVE_TIMEOUT = 10

PLATFORMS: list[Platform] = [
    Platform.BUTTON,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: WallDisplayConfigEntry) -> bool:
    """Set up one wall display."""
    client = WallDisplayClient(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PASSWORD),
    )
    try:
        device = await client.get_device_info()
    except WallDisplayError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": entry.data[CONF_HOST], "error": str(err)},
        ) from err
    if entry.unique_id and device.mac != entry.unique_id:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="wrong_device",
            translation_placeholders={"host": entry.data[CONF_HOST], "mac": device.mac},
        )

    speaker = Speaker(
        hass,
        client,
        entry.entry_id,
        entry.options.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS),
        entry.options.get(CONF_CACHE_MAX_MB, DEFAULT_CACHE_MAX_MB) * 1024 * 1024,
    )
    await speaker.async_load()

    coordinator = WallDisplayCoordinator(hass, entry, client, speaker)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = RuntimeData(
        client=client, device=device, coordinator=coordinator, speaker=speaker
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _cleanup(_now: datetime | None = None) -> None:
        try:
            deleted = await speaker.async_cleanup()
        except WallDisplayAuthError:
            entry.async_start_reauth(hass)
        except WallDisplayError as err:
            _LOGGER.debug("Clip cleanup skipped: %s", err)
        else:
            if deleted:
                _LOGGER.debug("Deleted %s old clips", deleted)

    entry.async_on_unload(async_track_time_interval(hass, _cleanup, CLEANUP_INTERVAL))
    entry.async_create_background_task(hass, _cleanup(), "wall_display_plus cleanup")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WallDisplayConfigEntry) -> bool:
    """Unload one wall display."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.speaker.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: WallDisplayConfigEntry) -> None:
    """Delete the clips from the display and the stored cache.

    One attempt with a short timeout: an unreachable display must not block
    the removal. The README names the "Clear speaker cache" button for that case.
    """
    client = WallDisplayClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data.get(CONF_PASSWORD)
    )
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cache")
    tag = (await store.async_load() or {}).get("tag")
    try:
        async with asyncio.timeout(REMOVE_TIMEOUT):
            for item in await client.list_media():
                if owns(item.filename, tag):
                    await client.delete_media(item.id)
    except (WallDisplayError, TimeoutError) as err:
        _LOGGER.debug("Clips stay on the display: %s", err)
    await store.async_remove()
