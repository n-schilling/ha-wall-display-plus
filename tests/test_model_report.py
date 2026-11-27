"""Wall Display X2i (experimental) and what diagnostics tell about a new model."""

from __future__ import annotations

from pytest_homeassistant.common import MockConfigEntry

from homeassistant.core import HomeAssistant

from custom_components.wall_display_plus.api import WallDisplayUnsupportedError
from custom_components.wall_display_plus.diagnostics import async_get_config_entry_diagnostics

from .conftest import FakeDisplay, fail


async def test_x2i_capabilities(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """An X2i is set up; diagnostics show what it offers, without lock settings."""
    display.model = "SAWD-5A1XX10EU0"
    display.has_motion = False
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert diag["device"]["model"] == "SAWD-5A1XX10EU0"
    assert set(diag["state"]["screen"]) == {"brightness", "screen_saver"}
    assert "lock_type" not in str(diag)
    assert diag["capabilities"] == {
        "temperatures": {"sht3x": -275.15, "thermal-cpufreq-0": 44.4},
        "cpu_temperature_key": "thermal-cpufreq-0",
    }


async def test_capabilities_with_motion_and_unknown_answers(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The motion configuration is shown; missing methods do not break diagnostics."""
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["capabilities"]["motion"] == {"enable": True, "wake_screen": True}

    fail(display, "get_internal_temperatures", WallDisplayUnsupportedError("-114"))
    fail(display, "get_motion_config", WallDisplayUnsupportedError("-114"))
    diag = await async_get_config_entry_diagnostics(hass, setup_entry)
    assert diag["capabilities"] == {
        "temperatures": "unavailable: WallDisplayUnsupportedError",
        "motion": "unavailable: WallDisplayUnsupportedError",
    }
