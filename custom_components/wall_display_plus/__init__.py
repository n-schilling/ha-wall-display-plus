"""Wall Display Plus: speaker and motion settings for a wall display."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.const import CONF_HOST, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .api import WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import CLEANUP_INTERVAL, CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS, DOMAIN, STORAGE_VERSION
from .coordinator import RuntimeData, WallDisplayConfigEntry, WallDisplayCoordinator
from .speaker import Speaker

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.MEDIA_PLAYER, Platform.SWITCH]


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

    coordinator = WallDisplayCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    speaker = Speaker(
        hass, client, entry.entry_id, entry.options.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS)
    )
    await speaker.async_load()

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
    """Remove the stored cache when the entry is deleted.

    The clips stay on the display; the "Clear speaker cache" button deletes
    them before removing the entry.
    """
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.cache").async_remove()
