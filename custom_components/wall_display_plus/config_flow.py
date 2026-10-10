"""Config flow for Wall Display Plus."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import DeviceInfo, WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import (
    CONF_CACHE_DAYS,
    CONF_CACHE_MAX_MB,
    DEFAULT_CACHE_DAYS,
    DEFAULT_CACHE_MAX_MB,
    DOMAIN,
    MAX_CACHE_DAYS,
    MAX_CACHE_MAX_MB,
    MIN_CACHE_DAYS,
    MIN_CACHE_MAX_MB,
)
from .coordinator import WallDisplayConfigEntry

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

# The display announces itself as "ShellyWallDisplay-<MAC>._shelly._tcp.local."
ZEROCONF_NAME = re.compile(r"^[^-]+-([0-9a-f]{12})\.", re.IGNORECASE)


class CannotConnect(Exception):
    """The display is not reachable."""


class NotAWallDisplay(Exception):
    """The device answered, but is no wall display."""


class InvalidAuth(Exception):
    """A password is needed or it is wrong."""


def _connection_schema() -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_HOST): str,
            probatio.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
        }
    )


class WallDisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a wall display."""

    VERSION = 1

    _discovered_host: str = ""
    _discovered_name: str = ""

    async def _async_validate(self, host: str, password: str | None) -> tuple[DeviceInfo, str]:
        """Check the device; return its information and a title."""
        client = WallDisplayClient(async_get_clientsession(self.hass), host, password)
        try:
            device = await client.get_device_info()
        except WallDisplayError as err:
            raise CannotConnect from err
        if not device.is_wall_display or not device.mac:
            raise NotAWallDisplay
        if device.auth_enabled and not password:
            raise InvalidAuth
        try:
            sys_config = await client.rpc("Sys.GetConfig")
        except WallDisplayAuthError as err:
            raise InvalidAuth from err
        except WallDisplayError as err:
            raise CannotConnect from err
        name = str((sys_config.get("device") or {}).get("name") or f"Wall display {host}")
        return device, name

    async def _async_try(
        self, host: str, password: str | None, errors: dict[str, str]
    ) -> tuple[DeviceInfo, str] | None:
        try:
            return await self._async_validate(host, password)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except NotAWallDisplay:
            errors["base"] = "not_wall_display"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the address and, if set on the display, the password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            password = user_input.get(CONF_PASSWORD) or None
            if result := await self._async_try(host, password, errors):
                device, name = result
                await self.async_set_unique_id(device.mac)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=name, data={CONF_HOST: host, CONF_PASSWORD: password}
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_connection_schema(), user_input),
            errors=errors,
        )

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A wall display announced itself on the network."""
        if discovery_info.ip_address.version == 6:
            return self.async_abort(reason="ipv6_not_supported")
        host = discovery_info.host
        if match := ZEROCONF_NAME.match(discovery_info.name):
            await self.async_set_unique_id(match.group(1).lower())
            if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, match.group(1).lower()
            ):
                return await self._async_known_display_moved(entry, host)
        name = discovery_info.name.split(".")[0]
        try:
            device, name = await self._async_validate(host, None)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        except NotAWallDisplay:
            return self.async_abort(reason="not_wall_display")
        except InvalidAuth:
            pass  # the password is asked for in the next step
        else:
            await self.async_set_unique_id(device.mac)
            if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
                DOMAIN, device.mac
            ):
                return await self._async_known_display_moved(entry, host)
        self._discovered_host = host
        self._discovered_name = name
        self.context["title_placeholders"] = {"name": name}
        return await self.async_step_zeroconf_confirm()

    async def _async_known_display_moved(
        self, entry: WallDisplayConfigEntry, host: str
    ) -> ConfigFlowResult:
        """Take a new address of a configured display only when it is safe.

        Anyone on the network can announce a display's name. Without a password
        the address is taken over only if the device at the new address reports
        the same MAC. With a password the address is never changed by discovery:
        the digest login would otherwise go to an unchecked device. The user
        changes it with Reconfigure instead.
        """
        if entry.data[CONF_HOST] == host or entry.data.get(CONF_PASSWORD):
            return self.async_abort(reason="already_configured")
        client = WallDisplayClient(async_get_clientsession(self.hass), host, None)
        try:
            device = await client.get_device_info()
        except WallDisplayError:
            return self.async_abort(reason="already_configured")
        if device.mac != entry.unique_id or not device.is_wall_display:
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        return self.async_abort(reason="already_configured")  # pragma: no cover

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered display; ask for the password if it has one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or None
            if result := await self._async_try(self._discovered_host, password, errors):
                device, name = result
                await self.async_set_unique_id(device.mac)
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._discovered_host})
                return self.async_create_entry(
                    title=name,
                    data={CONF_HOST: self._discovered_host, CONF_PASSWORD: password},
                )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=probatio.Schema({probatio.Optional(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={
                "name": self._discovered_name,
                "host": self._discovered_host,
            },
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the address or the password."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            password = user_input.get(CONF_PASSWORD) or None
            if result := await self._async_try(host, password, errors):
                await self.async_set_unique_id(result[0].mac)
                self._abort_if_unique_id_mismatch(reason="wrong_device")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_HOST: host, CONF_PASSWORD: password}
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema(), {CONF_HOST: entry.data[CONF_HOST]}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The display rejected the password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or None
            if await self._async_try(entry.data[CONF_HOST], password, errors):
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema({probatio.Optional(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: WallDisplayConfigEntry) -> OptionsFlowHandler:
        """Return the options flow."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    """How long clips stay cached and how much room they may take."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_CACHE_DAYS: int(user_input[CONF_CACHE_DAYS]),
                    CONF_CACHE_MAX_MB: int(user_input[CONF_CACHE_MAX_MB]),
                }
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_CACHE_DAYS,
                        default=options.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_CACHE_DAYS,
                            max=MAX_CACHE_DAYS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="d",
                        )
                    ),
                    probatio.Required(
                        CONF_CACHE_MAX_MB,
                        default=options.get(CONF_CACHE_MAX_MB, DEFAULT_CACHE_MAX_MB),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_CACHE_MAX_MB,
                            max=MAX_CACHE_MAX_MB,
                            step=10,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="MB",
                        )
                    ),
                }
            ),
        )
