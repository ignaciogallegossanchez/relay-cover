"""Fixtures for the Relay cover tests."""

from collections.abc import Iterator
from unittest.mock import patch

from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.relay_cover.const import (
    CONF_CLOSE_RELAY,
    CONF_CLOSING_TIME,
    CONF_END_MARGIN,
    CONF_HIDE_RELAYS,
    CONF_INTERLOCK,
    CONF_OPEN_RELAY,
    CONF_OPENING_TIME,
    DOMAIN,
)

from .common import CLOSE_RELAY, OPEN_RELAY, CoverTimers, restore_saved_position


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom integrations in every test (phcc needs it)."""


@pytest.fixture(autouse=True)
def cover_timers() -> Iterator[CoverTimers]:
    """Follow the timers of the covers (autouse: in place before any cover is set up)."""
    timers = CoverTimers()
    module = "custom_components.relay_cover.cover"
    with (
        patch(f"{module}.async_call_later", timers.call_later),
        patch(f"{module}.async_track_time_interval", timers.track_time_interval),
    ):
        yield timers


@pytest.fixture
def confirm_delay() -> float:
    """Seconds the fake device takes to confirm a command (negative: never)."""
    return 0


@pytest.fixture
def command_delay() -> float:
    """Seconds the relay service call itself takes."""
    return 0


@pytest.fixture
def interlock() -> bool:
    """Whether the cover uses the software interlock."""
    return True


@pytest.fixture
def end_margin() -> float:
    """Percent of a full travel added to an order to an end (0: none)."""
    return 0


@pytest.fixture
def hide_relays() -> bool:
    """Whether the cover hides its relays."""
    return True


@pytest.fixture
def opening_time() -> float:
    """Seconds of a full travel up."""
    return 20


@pytest.fixture
def closing_time() -> float:
    """Seconds of a full travel down."""
    return 30


@pytest.fixture
def saved_position() -> float | None:
    """Position of the cover saved before the test starts (None: fresh install)."""
    return 0


@pytest.fixture
async def relays(
    hass: HomeAssistant, confirm_delay: float, command_delay: float
) -> None:
    """Two fake relays, registered in the entity registry."""
    assert await async_setup_component(
        hass,
        Platform.SWITCH,
        {
            Platform.SWITCH: [
                {
                    "platform": "fake_relay",
                    "name": name,
                    "unique_id": name.lower(),
                    "confirm_delay": confirm_delay,
                    "command_delay": command_delay,
                }
                for name in ("Up", "Down")
            ]
        },
    )
    await hass.async_block_till_done()


@pytest.fixture
def mock_config_entry(
    interlock: bool,
    end_margin: float,
    hide_relays: bool,
    opening_time: float,
    closing_time: float,
) -> MockConfigEntry:
    """A relay cover entry as the config flow stores it."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        options={
            CONF_NAME: "Living room",
            CONF_OPEN_RELAY: OPEN_RELAY,
            CONF_CLOSE_RELAY: CLOSE_RELAY,
            CONF_OPENING_TIME: opening_time,
            CONF_CLOSING_TIME: closing_time,
            CONF_INTERLOCK: interlock,
            CONF_END_MARGIN: end_margin,
            CONF_HIDE_RELAYS: hide_relays,
        },
    )


@pytest.fixture
async def cover(
    hass: HomeAssistant,
    relays: None,
    mock_config_entry: MockConfigEntry,
    saved_position: float | None,
) -> MockConfigEntry:
    """The relay cover entry, set up from a saved position."""
    if saved_position is not None:
        restore_saved_position(hass, saved_position)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return mock_config_entry
