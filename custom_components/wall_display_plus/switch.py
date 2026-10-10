"""Switch: does motion wake the screen."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import WallDisplayError
from .const import DOMAIN
from .coordinator import WallDisplayConfigEntry
from .entity import WallDisplayEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WallDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch."""
    async_add_entities([MotionWakesScreenSwitch(entry)])


class MotionWakesScreenSwitch(WallDisplayEntity, SwitchEntity):
    """Whether motion in front of the display wakes the screen.

    Touch always wakes it. The value is written only when it really changes:
    every write makes the official integration for the display reload, and
    its entities are unavailable for a moment.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: WallDisplayConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(entry, "motion_wakes_screen")

    @property
    def is_on(self) -> bool | None:
        """Return whether motion wakes the screen."""
        return self.coordinator.data.wake_screen

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let motion wake the screen."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Only touch wakes the screen."""
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        client = self._entry.runtime_data.client
        try:
            current = (await client.get_motion_config()).get("wake_screen")
            if current != enabled:
                await client.set_wake_screen(enabled)
        except WallDisplayError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        self.coordinator.async_set_updated_data(replace(self.coordinator.data, wake_screen=enabled))
