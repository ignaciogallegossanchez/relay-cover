"""The Relay cover integration."""

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import logging
from typing import Any, Self

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.helper_integration import async_handle_source_entity_changes

from .const import (
    CONF_CLOSE_RELAY,
    CONF_CLOSING_TIME,
    CONF_END_MARGIN,
    CONF_HIDE_RELAYS,
    CONF_INTERLOCK,
    CONF_OPEN_RELAY,
    CONF_OPENING_TIME,
    DOMAIN,
    PLATFORMS,
)
from .motion import CONFIRM_TIMEOUT

_LOGGER = logging.getLogger(__name__)

RELAY_OPTIONS = (CONF_OPEN_RELAY, CONF_CLOSE_RELAY)


@dataclass(frozen=True, slots=True)
class RelayCoverConfig:
    """Validated options of a relay cover entry."""

    open_relay: str
    close_relay: str
    opening_time: float
    closing_time: float
    interlock: bool
    # Percent of a full travel
    end_margin: float
    hide_relays: bool

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> Self:
        """Build the configuration from the options of an entry."""
        return cls(
            open_relay=options[CONF_OPEN_RELAY],
            close_relay=options[CONF_CLOSE_RELAY],
            opening_time=options[CONF_OPENING_TIME],
            closing_time=options[CONF_CLOSING_TIME],
            interlock=options[CONF_INTERLOCK],
            end_margin=options[CONF_END_MARGIN],
            hide_relays=options[CONF_HIDE_RELAYS],
        )

    def relay(self, option_key: str) -> str:
        """Return the relay of an option: the open or the close relay."""
        return self.open_relay if option_key == CONF_OPEN_RELAY else self.close_relay

    @property
    def hidden_relays(self) -> frozenset[str]:
        """Return the relays this entry hides."""
        if not self.hide_relays:
            return frozenset()
        return frozenset((self.open_relay, self.close_relay))


type RelayCoverConfigEntry = ConfigEntry[RelayCoverConfig]


async def async_setup_entry(hass: HomeAssistant, entry: RelayCoverConfigEntry) -> bool:
    """Set up a relay cover from a config entry."""
    entry.runtime_data = RelayCoverConfig.from_options(entry.options)
    registry = er.async_get(hass)
    for option_key in RELAY_OPTIONS:
        relay = entry.options[option_key]
        # Another relay chosen in the options, or the removed one is back
        if relay in registry.entities or hass.states.get(relay) is not None:
            ir.async_delete_issue(
                hass, DOMAIN, _relay_removed_issue_id(entry, option_key)
            )
        entry.async_on_unload(_async_follow_relay(hass, entry, option_key))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: RelayCoverConfigEntry) -> bool:
    """Unload a config entry."""
    # async_unload_platforms_or_raise and a None return need Home Assistant 2026.11
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    old, new = entry.runtime_data, RelayCoverConfig.from_options(entry.options)
    # On a reload after the options changed, show the relays this entry no longer hides
    _async_show_relays(hass, old.hidden_relays - new.hidden_relays)
    # A relay that leaves its role would stay on with nobody to switch it off
    await _async_switch_off(
        hass,
        [
            old.relay(option_key)
            for option_key in RELAY_OPTIONS
            if old.relay(option_key) != new.relay(option_key)
        ],
    )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: RelayCoverConfigEntry) -> None:
    """Show again the relays the removed entry hid; drop its repair issues."""
    _async_show_relays(hass, RelayCoverConfig.from_options(entry.options).hidden_relays)
    for option_key in RELAY_OPTIONS:
        ir.async_delete_issue(hass, DOMAIN, _relay_removed_issue_id(entry, option_key))


@callback
def async_hide_relays(hass: HomeAssistant, entity_ids: Iterable[str]) -> None:
    """Hide relays nobody has hidden yet."""
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        if (
            registry_entry := registry.async_get(entity_id)
        ) is not None and registry_entry.hidden_by is None:
            registry.async_update_entity(
                entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
            )


@callback
def _async_show_relays(hass: HomeAssistant, entity_ids: Iterable[str]) -> None:
    """Show relays hidden by an integration; leave the ones the user hid.

    As the helpers of the core: the registry does not say which integration hid a relay.
    """
    registry = er.async_get(hass)
    for entity_id in entity_ids:
        if (
            registry_entry := registry.async_get(entity_id)
        ) is not None and registry_entry.hidden_by is er.RegistryEntryHider.INTEGRATION:
            registry.async_update_entity(entity_id, hidden_by=None)


async def async_switch_relay(
    hass: HomeAssistant, entity_id: str, turn_on: bool, context: Context | None = None
) -> None:
    """Switch a relay and wait for it.

    A call that hangs would hold back every later command: after `CONFIRM_TIMEOUT` it is
    taken as failed.
    """
    timeout = asyncio.timeout(CONFIRM_TIMEOUT)
    try:
        async with timeout:
            await hass.services.async_call(
                Platform.SWITCH,
                SERVICE_TURN_ON if turn_on else SERVICE_TURN_OFF,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
                context=context,
            )
    except TimeoutError as err:
        if not timeout.expired():
            raise
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="relay_timeout",
            translation_placeholders={"relay": entity_id},
        ) from err


async def _async_switch_off(hass: HomeAssistant, entity_ids: list[str]) -> None:
    """Switch off the relays Home Assistant shows on; log what fails."""
    for entity_id in entity_ids:
        if (state := hass.states.get(entity_id)) is None or state.state != STATE_ON:
            continue
        try:
            await async_switch_relay(hass, entity_id, turn_on=False)
        except Exception:
            # The unload must go on
            _LOGGER.exception("Could not switch off the relay %s", entity_id)


def _async_follow_relay(
    hass: HomeAssistant, entry: RelayCoverConfigEntry, option_key: str
) -> CALLBACK_TYPE:
    """Keep the entry in sync when a relay is renamed or removed."""
    relay = entry.options[option_key]

    def set_relay_entity_id(new_entity_id: str) -> None:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, option_key: new_entity_id}
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)

    async def relay_removed() -> None:
        # Without this callback the helper would unlink the cover from its own device
        ir.async_create_issue(
            hass,
            DOMAIN,
            _relay_removed_issue_id(entry, option_key),
            is_fixable=False,
            # Nothing would create it again after a restart
            is_persistent=True,
            severity=ir.IssueSeverity.ERROR,
            translation_key="relay_removed",
            translation_placeholders={"cover": entry.title, "relay": relay},
        )

    # No source device: the cover has its own device, not the device of a relay
    return async_handle_source_entity_changes(
        hass,
        helper_config_entry_id=entry.entry_id,
        set_source_entity_id_or_uuid=set_relay_entity_id,
        source_device_id=None,
        source_entity_id_or_uuid=relay,
        source_entity_removed=relay_removed,
    )


def _relay_removed_issue_id(entry: RelayCoverConfigEntry, option_key: str) -> str:
    return f"relay_removed_{entry.entry_id}_{option_key}"
