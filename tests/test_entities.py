"""Tests for setup, device linking, switch, button and diagnostics."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.wall_display_plus.diagnostics import async_get_config_entry_diagnostics

from .conftest import MAC, FakeDisplay


def _entity_id(hass: HomeAssistant, domain: str, key: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, "wall_display_plus", f"{MAC}_{key}")
    assert entity_id
    return entity_id


async def test_not_ready_when_unreachable(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """An unreachable display is retried later."""
    display.reachable = False
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_own_device_next_to_existing_one(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """The entities get their own device; the official one stays untouched."""
    other = MockConfigEntry(domain="shelly", unique_id=MAC)
    other.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    existing = device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "02:a1:b2:c3:d4:e5")},
        name="Hallway Wall Display XL",
        model="Shelly Wall Display XL",
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    ours = device_registry.async_get_device_by_identifier(
        ("wall_display_plus", MAC), config_entry.entry_id
    )
    assert ours is not None
    assert ours.id != existing.id
    assert ours.config_entry_id == config_entry.entry_id
    assert ours.connections == {(dr.CONNECTION_NETWORK_MAC, "02:a1:b2:c3:d4:e5")}
    assert ours.name == "Hallway Display"
    assert ours.model == "SAWD-3A1XE10EU2"
    registry = er.async_get(hass)
    for domain, key in (
        ("media_player", "speaker"),
        ("switch", "motion_wakes_screen"),
        ("button", "clear_cache"),
    ):
        assert registry.async_get(_entity_id(hass, domain, key)).device_id == ours.id
    unchanged = device_registry.async_get(existing.id)
    assert unchanged.name == "Hallway Wall Display XL"
    assert unchanged.model == "Shelly Wall Display XL"
    assert unchanged.config_entry_id == other.entry_id


async def test_switch_writes_only_on_change(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Turning the switch to its current value writes nothing to the display."""
    entity_id = _entity_id(hass, SWITCH_DOMAIN, "motion_wakes_screen")
    assert hass.states.get(entity_id).state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert display.motion_writes == 0

    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert display.motion_writes == 1
    assert display.wake_screen is False
    assert hass.states.get(entity_id).state == STATE_OFF

    # changed on the display meanwhile: the switch reads before writing
    display.wake_screen = True
    await hass.services.async_call(
        SWITCH_DOMAIN, SERVICE_TURN_ON, {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    assert display.motion_writes == 1
    assert hass.states.get(entity_id).state == STATE_ON


async def test_clear_cache_button(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """The button deletes our clips and nothing else."""
    await display.upload("wdp_a.mp3", "ALERT", b"x")
    await display.upload("wdp_b.mp3", "ALERT", b"x")
    setup_entry.runtime_data.speaker.entries["k"] = {
        "filename": "wdp_a.mp3",
        "last_used": "2026-10-10T00:00:00+00:00",
    }
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: _entity_id(hass, BUTTON_DOMAIN, "clear_cache")},
        blocking=True,
    )
    assert display.clips() == []
    assert display.by_name("song.mp3") is not None
    assert setup_entry.runtime_data.speaker.entries == {}


async def test_diagnostics_hide_password(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """Diagnostics never contain the password."""
    config_entry = MockConfigEntry(
        domain=config_entry.domain,
        unique_id=MAC,
        title=config_entry.title,
        data={**config_entry.data, "password": "secret"},
    )
    config_entry.add_to_hass(hass)
    display.auth_enabled = True
    display.password = "secret"
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    assert "secret" not in str(diag)
    assert diag["device"]["mac"] == MAC


async def test_unload(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """The entry unloads cleanly."""
    assert await hass.config_entries.async_unload(setup_entry.entry_id)
    assert setup_entry.state is ConfigEntryState.NOT_LOADED
