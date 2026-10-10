"""Config flow for Wall Display Plus."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

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

from .api import DeviceInfo, WallDisplayAuthError, WallDisplayClient, WallDisplayError
from .const import CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS, DOMAIN, MAX_CACHE_DAYS, MIN_CACHE_DAYS
from .coordinator import WallDisplayConfigEntry

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class CannotConnect(Exception):
    """The display is not reachable."""


class NotAWallDisplay(Exception):
    """The device answered, but is no wall display."""


class InvalidAuth(Exception):
    """A password is needed or it is wrong."""


class WallDisplayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a wall display."""

    VERSION = 1

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
        name = (sys_config.get("device") or {}).get("name") or f"Wall display {host}"
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
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                user_input,
            ),
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
                vol.Schema(
                    {
                        vol.Required(CONF_HOST): str,
                        vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR,
                    }
                ),
                {CONF_HOST: entry.data[CONF_HOST]},
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
            data_schema=vol.Schema({vol.Optional(CONF_PASSWORD): PASSWORD_SELECTOR}),
            description_placeholders={"host": entry.data[CONF_HOST]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: WallDisplayConfigEntry) -> OptionsFlowHandler:
        """Return the options flow."""
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    """How long clips stay cached."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options."""
        if user_input is not None:
            return self.async_create_entry(data={CONF_CACHE_DAYS: int(user_input[CONF_CACHE_DAYS])})
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CACHE_DAYS,
                        default=self.config_entry.options.get(CONF_CACHE_DAYS, DEFAULT_CACHE_DAYS),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_CACHE_DAYS,
                            max=MAX_CACHE_DAYS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="d",
                        )
                    ),
                }
            ),
        )
