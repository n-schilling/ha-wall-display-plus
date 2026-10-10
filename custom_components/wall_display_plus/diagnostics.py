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

from .api import WallDisplayError
from .coordinator import WallDisplayConfigEntry
from .speaker import owns, parse_timestamp

PLAYBACK_FIELDS = ("enable", "buffering", "volume", "media_type")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WallDisplayConfigEntry
) -> dict[str, Any]:
    """Return what helps to understand a problem, and nothing personal."""
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
            "wake_screen": data.wake_screen,
            "playback": {k: playback[k] for k in PLAYBACK_FIELDS if k in playback},
            "item_counts": data.media_status.get("item_counts"),
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
    }
