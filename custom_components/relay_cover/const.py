"""Constants for the Relay cover integration."""

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "relay_cover"
PLATFORMS: Final = [Platform.COVER]

CONF_OPEN_RELAY: Final = "open_relay"
CONF_CLOSE_RELAY: Final = "close_relay"
CONF_OPENING_TIME: Final = "opening_time"
CONF_CLOSING_TIME: Final = "closing_time"
CONF_INTERLOCK: Final = "interlock"
CONF_END_MARGIN: Final = "end_margin"
CONF_HIDE_RELAYS: Final = "hide_relays"

DEFAULT_TRAVEL_TIME: Final = 30
# Percent of a full travel
DEFAULT_END_MARGIN: Final = 20
