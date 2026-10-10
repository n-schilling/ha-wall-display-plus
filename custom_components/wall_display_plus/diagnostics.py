"""Diagnostics for Wall Display Plus."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .api import WallDisplayError
from .const import CLIP_PREFIX
from .coordinator import WallDisplayConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WallDisplayConfigEntry
) -> dict[str, Any]:
    """Return what helps to understand a problem, without the password."""
    runtime = entry.runtime_data
    try:
        library = [
            asdict(item)
            for item in await runtime.client.list_media()
            if item.filename.startswith(CLIP_PREFIX)
        ]
    except WallDisplayError as err:
        library = [f"unavailable: {err}"]
    return {
        "entry": async_redact_data(dict(entry.data), {CONF_PASSWORD}),
        "options": dict(entry.options),
        "device": asdict(runtime.device),
        "state": asdict(runtime.coordinator.data) if runtime.coordinator.data else None,
        "cache_days": runtime.speaker.cache_days,
        "cache_entries": runtime.speaker.entries,
        "clips_on_display": library,
    }
