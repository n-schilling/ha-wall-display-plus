"""Tests for the config flow."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.wall_display_plus.const import CONF_CACHE_DAYS, DOMAIN

from .conftest import HOST, MAC, FakeDisplay


async def _start(hass: HomeAssistant, data: dict) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def test_user_creates_entry(hass: HomeAssistant, display: FakeDisplay) -> None:
    """A reachable display without password is added under its own name."""
    result = await _start(hass, {CONF_HOST: f" {HOST} "})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hallway Display"
    assert result["data"] == {CONF_HOST: HOST, CONF_PASSWORD: None}
    assert result["result"].unique_id == MAC


async def test_user_cannot_connect(hass: HomeAssistant, display: FakeDisplay) -> None:
    """An unreachable host shows an error."""
    display.reachable = False
    result = await _start(hass, {CONF_HOST: HOST})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_not_a_wall_display(hass: HomeAssistant, display: FakeDisplay) -> None:
    """Another device is refused."""
    display.app = "Plus1PM"
    display.model = "SNSW-001P16EU"
    result = await _start(hass, {CONF_HOST: HOST})
    assert result["errors"] == {"base": "not_wall_display"}


async def test_user_password(hass: HomeAssistant, display: FakeDisplay) -> None:
    """A display with a password needs the right one."""
    display.auth_enabled = True
    display.password = "secret"
    result = await _start(hass, {CONF_HOST: HOST})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await _start(hass, {CONF_HOST: HOST, CONF_PASSWORD: "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await _start(hass, {CONF_HOST: HOST, CONF_PASSWORD: "secret"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == "secret"


async def test_user_already_configured(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """The same display cannot be added twice; a new address is taken over."""
    config_entry.add_to_hass(hass)
    result = await _start(hass, {CONF_HOST: "192.0.2.11"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "192.0.2.11"


async def test_reconfigure(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """The address can be changed."""
    result = await setup_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.19"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert setup_entry.data[CONF_HOST] == "192.0.2.19"


async def test_reauth(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """A new password is asked for and stored."""
    display.auth_enabled = True
    display.password = "new"
    result = await setup_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "bad"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert setup_entry.data[CONF_PASSWORD] == "new"


async def test_options(hass: HomeAssistant, setup_entry: MockConfigEntry) -> None:
    """The cache duration can be changed and reaches the speaker."""
    result = await hass.config_entries.options.async_init(setup_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CACHE_DAYS: 30}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert setup_entry.options == {CONF_CACHE_DAYS: 30}
    assert setup_entry.runtime_data.speaker.cache_days == 30
