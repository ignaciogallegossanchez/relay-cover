"""Fake relay used only by the tests.

It behaves like a device integration: the device confirms commands after an optional
delay, and its wall button pushes a new state with a fresh context.
"""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

DOMAIN = "fake_relay"
SIGNAL_WALL_BUTTON = f"{DOMAIN}_wall_button"


@callback
def async_press_wall_button(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    """Switch the relay from the wall: the device reports the new state on its own."""
    async_dispatcher_send(hass, SIGNAL_WALL_BUTTON, entity_id, on)
