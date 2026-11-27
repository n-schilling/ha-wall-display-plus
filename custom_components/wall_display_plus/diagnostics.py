"""Diagnostics for Wall Display Plus.

People attach diagnostics to public issues, so they hold only what helps to
find a problem: no password, address or MAC, no cache keys, file names or
timestamps of announcements (they would tell when which announcement ran),
and nothing about what the display played (titles, artists, pictures).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import WallDisplayClient, WallDisplayError, cpu_temperature_key
from .const import CONF_MEMBERS
from .coordinator import WallDisplayConfigEntry, is_group
from .speaker import owns, parse_timestamp

PLAYBACK_FIELDS = ("enable", "buffering", "volume", "media_type")
# Parts of the screen configuration this integration reads and writes; the
# lock settings stay out.
SCREEN_FIELDS = ("brightness", "screen_saver")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WallDisplayConfigEntry
) -> dict[str, Any]:
    """Return what helps to understand a problem, and nothing personal."""
    if is_group(entry):
        return {"type": "group", "members": len(entry.options.get(CONF_MEMBERS, []))}
    runtime = entry.runtime_data
    clips: dict[str, Any] | str
    try:
        ours = [
            i for i in await runtime.client.list_media() if owns(i.filename, runtime.speaker.tag)
        ]
    except WallDisplayError as err:
        clips = f"unavailable: {type(err).__name__}"
    else:
        clips = {
            "count": len(ours),
            "bytes": sum(i.size for i in ours),
            "types": dict(Counter(i.type for i in ours)),
        }

    data = runtime.coordinator.data
    state: dict[str, Any] | None = None
    if data is not None:
        playback = data.media_status.get("playback") or {}
        state = {
            "has_motion": runtime.coordinator.has_motion,
            "wake_screen": data.wake_screen,
            "playback": {k: playback[k] for k in PLAYBACK_FIELDS if k in playback},
            "item_counts": data.media_status.get("item_counts"),
            "screen": {k: data.ui_config[k] for k in SCREEN_FIELDS if k in data.ui_config},
        }

    now = dt_util.utcnow()
    ages = sorted(
        (now - parse_timestamp(e.get("last_used"))).days for e in runtime.speaker.entries.values()
    )
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_HOST, CONF_PASSWORD}),
        "options": dict(entry.options),
        "device": async_redact_data(asdict(runtime.device), {"mac"}),
        "state": state,
        "cache": {
            "days": runtime.speaker.cache_days,
            "max_bytes": runtime.speaker.cache_max_bytes,
            "entries": len(ages),
            "newest_days": ages[0] if ages else None,
            "oldest_days": ages[-1] if ages else None,
        },
        "clips_on_display": clips,
        "capabilities": await _capabilities(runtime.client, runtime.coordinator.has_motion),
    }


async def _capabilities(client: WallDisplayClient, has_motion: bool | None) -> dict[str, Any]:
    """Ask the display what it offers, for models nobody of the project owns."""
    result: dict[str, Any] = {}
    try:
        temperatures = await client.get_internal_temperatures()
    except WallDisplayError as err:
        result["temperatures"] = f"unavailable: {type(err).__name__}"
    else:
        result["temperatures"] = temperatures
        result["cpu_temperature_key"] = cpu_temperature_key(temperatures)
    if has_motion:
        try:
            result["motion"] = await client.get_motion_config()
        except WallDisplayError as err:
            result["motion"] = f"unavailable: {type(err).__name__}"
    return result
