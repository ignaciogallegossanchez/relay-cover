"""Switch platform of the fake relay."""

import asyncio
from datetime import datetime
from typing import Any, override

from homeassistant.components.switch import (
    PLATFORM_SCHEMA as SWITCH_PLATFORM_SCHEMA,
    SwitchEntity,
)
from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
import probatio

from . import SIGNAL_WALL_BUTTON

CONF_CONFIRM_DELAY = "confirm_delay"
CONF_COMMAND_DELAY = "command_delay"

PLATFORM_SCHEMA = SWITCH_PLATFORM_SCHEMA.extend(
    {
        probatio.Required(CONF_NAME): cv.string,
        probatio.Optional(CONF_UNIQUE_ID): cv.string,
        probatio.Optional(CONF_CONFIRM_DELAY, default=0): probatio.Coerce(float),
        probatio.Optional(CONF_COMMAND_DELAY, default=0): probatio.Coerce(float),
    }
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up one fake relay from YAML."""
    async_add_entities(
        [
            FakeRelay(
                config[CONF_NAME],
                config.get(CONF_UNIQUE_ID),
                config[CONF_CONFIRM_DELAY],
                config[CONF_COMMAND_DELAY],
            )
        ]
    )


class FakeRelay(SwitchEntity):
    """A relay whose device confirms commands after a delay (never if negative).

    A new command cancels a confirmation still pending: it does not queue commands.
    """

    _attr_should_poll = False
    _attr_is_on = False

    def __init__(
        self,
        name: str,
        unique_id: str | None,
        confirm_delay: float,
        command_delay: float,
    ) -> None:
        """Initialize the relay; command_delay makes the service call itself slow."""
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._confirm_delay = confirm_delay
        self._command_delay = command_delay
        self._cancel_confirm: CALLBACK_TYPE | None = None

    @override
    async def async_added_to_hass(self) -> None:
        """Listen to the wall button."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_WALL_BUTTON, self._async_wall_button
            )
        )
        self.async_on_remove(self._async_cancel_confirm)

    @callback
    def _async_cancel_confirm(self) -> None:
        if self._cancel_confirm is not None:
            self._cancel_confirm()
            self._cancel_confirm = None

    @callback
    def _async_wall_button(self, entity_id: str, on: bool) -> None:
        if entity_id != self.entity_id:
            return
        self._attr_is_on = on
        self.async_write_ha_state()

    @callback
    def _async_command(self, on: bool) -> None:
        self._async_cancel_confirm()
        if self._confirm_delay < 0:
            return
        if not self._confirm_delay:
            self._attr_is_on = on
            self.async_write_ha_state()
            return

        @callback
        def _confirm(_now: datetime) -> None:
            self._cancel_confirm = None
            self._attr_is_on = on
            self.async_write_ha_state()

        self._cancel_confirm = async_call_later(
            self.hass, self._confirm_delay, _confirm
        )

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Ask the device to turn on."""
        await asyncio.sleep(self._command_delay)
        self._async_command(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Ask the device to turn off."""
        await asyncio.sleep(self._command_delay)
        self._async_command(False)
