"""Tests for the config flow."""

from __future__ import annotations

from dataclasses import replace
from ipaddress import ip_address
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.wall_display_plus.api import WallDisplayError
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


def _zeroconf(
    host: str = HOST, name: str = "ShellyWallDisplay-02A1B2C3D4E5"
) -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname="localhost.local.",
        name=f"{name}._shelly._tcp.local.",
        port=80,
        type="_shelly._tcp.local.",
        properties={"app": "WallDisplay", "gen": "2"},
    )


async def test_zeroconf_new_display(hass: HomeAssistant, display: FakeDisplay) -> None:
    """A discovered display is confirmed and added under its own name."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"] == {
        "name": "Hallway Display",
        "host": HOST,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_HOST: HOST, CONF_PASSWORD: None}
    assert result["result"].unique_id == MAC


async def test_zeroconf_updates_known_display(
    hass: HomeAssistant, display: FakeDisplay, config_entry: MockConfigEntry
) -> None:
    """A known display with a new address only updates the address."""
    config_entry.add_to_hass(hass)
    display.reachable = False  # no connection needed: the MAC is in the name
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf("192.0.2.77")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "192.0.2.77"


async def test_zeroconf_name_without_mac(hass: HomeAssistant, display: FakeDisplay) -> None:
    """Without a MAC in the name the device is asked."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_zeroconf(name="Kitchen Display"),
    )
    assert result["step_id"] == "zeroconf_confirm"


async def test_zeroconf_with_password(hass: HomeAssistant, display: FakeDisplay) -> None:
    """A display with a password asks for it in the confirm step."""
    display.auth_enabled = True
    display.password = "secret"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["name"] == "ShellyWallDisplay-02A1B2C3D4E5"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "secret"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == "secret"


@pytest.mark.parametrize(
    ("change", "reason"),
    [("unreachable", "cannot_connect"), ("other_device", "not_wall_display")],
)
async def test_zeroconf_aborts(
    hass: HomeAssistant, display: FakeDisplay, change: str, reason: str
) -> None:
    """Unreachable or foreign devices end the discovery."""
    if change == "unreachable":
        display.reachable = False
    else:
        display.app = "Plus1PM"
        display.model = "SNSW-001P16EU"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason


async def test_zeroconf_ipv6(hass: HomeAssistant, display: FakeDisplay) -> None:
    """IPv6 announcements are ignored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_ZEROCONF}, data=_zeroconf("fe80::1")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "ipv6_not_supported"


async def test_user_recovers_after_error(hass: HomeAssistant, display: FakeDisplay) -> None:
    """After an error the same form leads to an entry."""
    display.reachable = False
    result = await _start(hass, {CONF_HOST: HOST})
    assert result["errors"] == {"base": "cannot_connect"}
    display.reachable = True
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOST: HOST})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_rpc_unreachable_after_device_info(hass: HomeAssistant, display: FakeDisplay) -> None:
    """A display that answers /shelly but not RPC cannot be added."""
    original = display.rpc

    async def failing_rpc(*args, **kwargs):
        raise WallDisplayError("rpc down")

    display.rpc = failing_rpc  # type: ignore[method-assign]
    result = await _start(hass, {CONF_HOST: HOST})
    assert result["errors"] == {"base": "cannot_connect"}
    display.rpc = original  # type: ignore[method-assign]


async def test_reconfigure_errors(
    hass: HomeAssistant, display: FakeDisplay, setup_entry: MockConfigEntry
) -> None:
    """Reconfigure shows errors and refuses another display."""
    result = await setup_entry.start_reconfigure_flow(hass)
    display.reachable = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.5"}
    )
    assert result["errors"] == {"base": "cannot_connect"}
    display.reachable = True
    with patch("custom_components.wall_display_plus.config_flow.WallDisplayClient") as factory:
        other = FakeDisplay()
        other.get_device_info = _other_mac(other.get_device_info)  # type: ignore[method-assign]
        factory.return_value = other
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_HOST: "192.0.2.5"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert setup_entry.data[CONF_HOST] == HOST


def _other_mac(original):
    async def get_device_info():
        info = await original()
        return replace(info, mac="aabbccddeeff")

    return get_device_info
