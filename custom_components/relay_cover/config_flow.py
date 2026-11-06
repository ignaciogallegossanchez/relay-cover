"""Config flow for the Relay cover integration."""

from collections.abc import Mapping
from typing import Any, cast, override

from homeassistant.const import CONF_NAME, PERCENTAGE, Platform, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.schema_config_entry_flow import (
    SchemaCommonFlowHandler,
    SchemaConfigFlowHandler,
    SchemaFlowError,
    SchemaFlowFormStep,
    SchemaOptionsFlowHandler,
)
import probatio

from . import RelayCoverConfig, async_hide_relays
from .const import (
    CONF_CLOSE_RELAY,
    CONF_CLOSING_TIME,
    CONF_END_MARGIN,
    CONF_HIDE_RELAYS,
    CONF_INTERLOCK,
    CONF_OPEN_RELAY,
    CONF_OPENING_TIME,
    DEFAULT_END_MARGIN,
    DEFAULT_TRAVEL_TIME,
    DOMAIN,
)

RELAY_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=Platform.SWITCH)
)

TIME_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        step="any",
        unit_of_measurement=UnitOfTime.SECONDS,
        mode=selector.NumberSelectorMode.BOX,
    )
)

MARGIN_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0,
        max=100,
        step=1,
        unit_of_measurement=PERCENTAGE,
        mode=selector.NumberSelectorMode.BOX,
    )
)

OPTIONS_SCHEMA = {
    probatio.Required(CONF_OPEN_RELAY): RELAY_SELECTOR,
    probatio.Required(CONF_CLOSE_RELAY): RELAY_SELECTOR,
    probatio.Required(CONF_OPENING_TIME, default=DEFAULT_TRAVEL_TIME): TIME_SELECTOR,
    probatio.Required(CONF_CLOSING_TIME, default=DEFAULT_TRAVEL_TIME): TIME_SELECTOR,
    probatio.Required(CONF_INTERLOCK, default=True): selector.BooleanSelector(),
    probatio.Required(CONF_END_MARGIN, default=DEFAULT_END_MARGIN): MARGIN_SELECTOR,
    probatio.Required(CONF_HIDE_RELAYS, default=True): selector.BooleanSelector(),
}

CONFIG_SCHEMA = {
    probatio.Required(CONF_NAME): selector.TextSelector(),
    **OPTIONS_SCHEMA,
}


async def _validate_options(
    handler: SchemaCommonFlowHandler, user_input: dict[str, Any]
) -> dict[str, Any]:
    """Check the travel times and that the relays are different and not in use."""
    if user_input[CONF_OPENING_TIME] <= 0 or user_input[CONF_CLOSING_TIME] <= 0:
        raise SchemaFlowError("invalid_travel_time")
    relays = {user_input[CONF_OPEN_RELAY], user_input[CONF_CLOSE_RELAY]}
    if len(relays) == 1:
        raise SchemaFlowError("same_relay")

    flow = handler.parent_handler
    own_entry_id = (
        flow.config_entry.entry_id
        if isinstance(flow, SchemaOptionsFlowHandler)
        else None
    )
    for entry in flow.hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == own_entry_id:
            continue
        if relays & {entry.options[CONF_OPEN_RELAY], entry.options[CONF_CLOSE_RELAY]}:
            raise SchemaFlowError("relay_in_use")

    return user_input


CONFIG_FLOW = {
    "user": SchemaFlowFormStep(
        probatio.Schema(CONFIG_SCHEMA), validate_user_input=_validate_options
    ),
}

OPTIONS_FLOW = {
    "init": SchemaFlowFormStep(
        probatio.Schema(OPTIONS_SCHEMA), validate_user_input=_validate_options
    ),
}


class RelayCoverConfigFlowHandler(SchemaConfigFlowHandler, domain=DOMAIN):
    """Handle a config or options flow for Relay cover."""

    config_flow = CONFIG_FLOW
    options_flow = OPTIONS_FLOW
    options_flow_reloads = True

    @override
    def async_config_entry_title(self, options: Mapping[str, Any]) -> str:
        """Return the title of the config entry."""
        return cast(str, options[CONF_NAME])

    # Only here, not on every setup: a relay the user shows again stays shown
    @callback
    @override
    def async_config_flow_finished(self, options: Mapping[str, Any]) -> None:
        """Hide the relays if requested."""
        async_hide_relays(
            self.hass, RelayCoverConfig.from_options(options).hidden_relays
        )

    # The reload shows the relays no longer hidden: only it knows the old options
    @callback
    @staticmethod
    @override
    def async_options_flow_finished(
        hass: HomeAssistant, options: Mapping[str, Any]
    ) -> None:
        """Hide the relays if requested."""
        async_hide_relays(hass, RelayCoverConfig.from_options(options).hidden_relays)
