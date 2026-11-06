"""Config flow, options flow, relay hiding, device, relay tracking and repair issues."""

from typing import Any

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_NAME,
    EVENT_CALL_SERVICE,
    SERVICE_OPEN_COVER,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_UNAVAILABLE,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.relay_cover.const import (
    CONF_CLOSE_RELAY,
    CONF_CLOSING_TIME,
    CONF_END_MARGIN,
    CONF_HIDE_RELAYS,
    CONF_INTERLOCK,
    CONF_OPEN_RELAY,
    CONF_OPENING_TIME,
    DEFAULT_END_MARGIN,
    DOMAIN,
)

from .common import CLOSE_RELAY, COVER, OPEN_RELAY

USER_INPUT: dict[str, Any] = {
    CONF_NAME: "Living room",
    CONF_OPEN_RELAY: OPEN_RELAY,
    CONF_CLOSE_RELAY: CLOSE_RELAY,
    CONF_OPENING_TIME: 20.5,
    CONF_CLOSING_TIME: 30,
    CONF_INTERLOCK: True,
    CONF_HIDE_RELAYS: True,
}
OPTIONS_INPUT = {k: v for k, v in USER_INPUT.items() if k != CONF_NAME}


def _hidden_by(
    entity_registry: er.EntityRegistry, entity_id: str
) -> er.RegistryEntryHider | None:
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    return registry_entry.hidden_by


async def _create_with_flow(hass: HomeAssistant, **changes: Any) -> MockConfigEntry:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, **changes}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result["result"]


@pytest.fixture
async def flow_cover(hass: HomeAssistant, relays: None) -> MockConfigEntry:
    """The relay cover entry, created with the config flow (it hides the relays)."""
    return await _create_with_flow(hass)


async def _configure_options(
    hass: HomeAssistant, entry: MockConfigEntry, **changes: Any
) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**OPTIONS_INPUT, **changes}
    )
    await hass.async_block_till_done()
    return dict(result)


@pytest.mark.usefixtures("relays")
async def test_user_flow_creates_cover(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """The flow creates an entry with everything in options, and the entry a cover.

    The margin at the ends, left out, is 20 %.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living room"
    entry = result["result"]
    assert entry.data == {}
    assert entry.options == {**USER_INPUT, CONF_END_MARGIN: DEFAULT_END_MARGIN}
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.opening_time == 20.5
    assert entry.runtime_data.end_margin == 20

    state = hass.states.get(COVER)
    assert state is not None
    assert state.attributes["friendly_name"] == "Living room"
    assert state.attributes["device_class"] == "shutter"
    assert state.attributes["assumed_state"] is True
    registry_entry = entity_registry.async_get(COVER)
    assert registry_entry is not None
    assert registry_entry.unique_id == entry.entry_id


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        pytest.param({CONF_CLOSE_RELAY: OPEN_RELAY}, "same_relay", id="same_relay"),
        pytest.param({CONF_OPENING_TIME: 0}, "invalid_travel_time", id="zero_time"),
    ],
)
@pytest.mark.usefixtures("relays")
async def test_user_flow_rejects_bad_input(
    hass: HomeAssistant, changes: dict[str, Any], error: str
) -> None:
    """Equal relays and travel times of zero or less are refused; the user can fix it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, **changes}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.usefixtures("relays")
async def test_negative_travel_time_fails_the_schema(hass: HomeAssistant) -> None:
    """The time selector itself refuses negative values (the form shows min 0)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_CLOSING_TIME: -1}
        )


@pytest.mark.parametrize("margin", [0, 100])
@pytest.mark.usefixtures("relays")
async def test_user_flow_takes_the_limits_of_the_end_margin(
    hass: HomeAssistant, margin: int
) -> None:
    """0 % and 100 % are valid margins at the ends."""
    entry = await _create_with_flow(hass, **{CONF_END_MARGIN: margin})
    assert entry.options[CONF_END_MARGIN] == margin
    assert entry.runtime_data.end_margin == margin


@pytest.mark.parametrize("margin", [-1, 101])
@pytest.mark.usefixtures("relays")
async def test_end_margin_out_of_range_fails_the_schema(
    hass: HomeAssistant, margin: int
) -> None:
    """The percent selector refuses a margin below 0 % or above 100 %."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_END_MARGIN: margin}
        )


@pytest.mark.parametrize(
    "relay",
    [
        pytest.param({CONF_CLOSE_RELAY: "switch.other"}, id="open_relay_in_use"),
        pytest.param({CONF_OPEN_RELAY: "switch.other"}, id="close_relay_in_use"),
    ],
)
@pytest.mark.usefixtures("relays")
async def test_relay_in_use_is_rejected(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, relay: dict[str, str]
) -> None:
    """A relay can only drive one cover, whatever role it has there."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_NAME: "Other", **relay}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "relay_in_use"}


async def test_options_flow_reloads_entry(
    hass: HomeAssistant, cover: MockConfigEntry
) -> None:
    """Changing the options keeps the name, updates and reloads the entry."""
    result = await _configure_options(
        hass, cover, **{CONF_OPENING_TIME: 35, CONF_INTERLOCK: False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert cover.options[CONF_OPENING_TIME] == 35
    assert cover.options[CONF_NAME] == "Living room"
    assert cover.runtime_data.opening_time == 35
    assert cover.runtime_data.interlock is False
    assert cover.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("margin", [0, 100])
async def test_options_flow_changes_the_end_margin(
    hass: HomeAssistant, cover: MockConfigEntry, margin: int
) -> None:
    """The options flow takes the limits of the margin at the ends."""
    result = await _configure_options(hass, cover, **{CONF_END_MARGIN: margin})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert cover.options[CONF_END_MARGIN] == margin
    assert cover.runtime_data.end_margin == margin


@pytest.mark.parametrize("margin", [-1, 101])
async def test_options_flow_refuses_an_end_margin_out_of_range(
    hass: HomeAssistant, cover: MockConfigEntry, margin: int
) -> None:
    """The options flow refuses a margin out of 0-100 % and keeps the old one."""
    result = await hass.config_entries.options.async_init(cover.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {**OPTIONS_INPUT, CONF_END_MARGIN: margin}
        )
    assert cover.options[CONF_END_MARGIN] == 0


async def test_options_flow_keeps_own_relays_and_rejects_others(
    hass: HomeAssistant, cover: MockConfigEntry
) -> None:
    """The entry's own relays are not "in use"; another entry's relays are."""
    MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen",
        options={
            **cover.options,
            CONF_NAME: "Kitchen",
            CONF_OPEN_RELAY: "switch.kitchen_up",
            CONF_CLOSE_RELAY: "switch.kitchen_down",
        },
    ).add_to_hass(hass)

    result = await _configure_options(hass, cover)
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await _configure_options(
        hass, cover, **{CONF_OPEN_RELAY: "switch.kitchen_up"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "relay_in_use"}


async def test_relays_hidden_and_shown_again(
    hass: HomeAssistant, flow_cover: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Hidden by the config flow; shown again when the option is turned off."""
    for relay in (OPEN_RELAY, CLOSE_RELAY):
        assert _hidden_by(entity_registry, relay) is er.RegistryEntryHider.INTEGRATION

    await _configure_options(hass, flow_cover, **{CONF_HIDE_RELAYS: False})
    for relay in (OPEN_RELAY, CLOSE_RELAY):
        assert _hidden_by(entity_registry, relay) is None

    await _configure_options(hass, flow_cover, **{CONF_HIDE_RELAYS: True})
    for relay in (OPEN_RELAY, CLOSE_RELAY):
        assert _hidden_by(entity_registry, relay) is er.RegistryEntryHider.INTEGRATION


@pytest.mark.usefixtures("relays")
async def test_relays_not_hidden_when_option_off(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """With the option off the relays stay as they are."""
    await _create_with_flow(hass, **{CONF_HIDE_RELAYS: False})
    for relay in (OPEN_RELAY, CLOSE_RELAY):
        assert _hidden_by(entity_registry, relay) is None


async def test_setup_does_not_hide_relays(
    hass: HomeAssistant, cover: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Setting up an entry hides nothing: only the flows do."""
    for relay in (OPEN_RELAY, CLOSE_RELAY):
        assert _hidden_by(entity_registry, relay) is None


async def test_relay_shown_by_user_stays_shown(
    hass: HomeAssistant, flow_cover: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """The user shows a relay again: a reload or a new setup does not hide it."""
    entity_registry.async_update_entity(OPEN_RELAY, hidden_by=None)
    assert await hass.config_entries.async_reload(flow_cover.entry_id)
    await hass.async_block_till_done()
    assert _hidden_by(entity_registry, OPEN_RELAY) is None

    assert await hass.config_entries.async_unload(flow_cover.entry_id)
    assert await hass.config_entries.async_setup(flow_cover.entry_id)
    await hass.async_block_till_done()
    assert _hidden_by(entity_registry, OPEN_RELAY) is None
    assert _hidden_by(entity_registry, CLOSE_RELAY) is er.RegistryEntryHider.INTEGRATION


@pytest.mark.usefixtures("relays")
async def test_relay_hidden_by_user_is_left_alone(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """A relay the user hid stays hidden by the user, also after removing the entry."""
    entity_registry.async_update_entity(
        OPEN_RELAY, hidden_by=er.RegistryEntryHider.USER
    )
    entry = await _create_with_flow(hass)
    assert _hidden_by(entity_registry, OPEN_RELAY) is er.RegistryEntryHider.USER
    assert _hidden_by(entity_registry, CLOSE_RELAY) is er.RegistryEntryHider.INTEGRATION

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _hidden_by(entity_registry, OPEN_RELAY) is er.RegistryEntryHider.USER
    assert _hidden_by(entity_registry, CLOSE_RELAY) is None


async def test_replaced_relay_is_shown_again(
    hass: HomeAssistant, flow_cover: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Choosing another relay in the options shows the old one and hides the new one."""
    entity_registry.async_get_or_create(Platform.SWITCH, "test", "spare")
    spare = entity_registry.async_get_entity_id(Platform.SWITCH, "test", "spare")
    assert spare is not None

    await _configure_options(hass, flow_cover, **{CONF_CLOSE_RELAY: spare})
    assert flow_cover.options[CONF_CLOSE_RELAY] == spare
    assert _hidden_by(entity_registry, CLOSE_RELAY) is None
    assert _hidden_by(entity_registry, spare) is er.RegistryEntryHider.INTEGRATION
    assert _hidden_by(entity_registry, OPEN_RELAY) is er.RegistryEntryHider.INTEGRATION


async def test_remove_entry_shows_relays_and_removes_device(
    hass: HomeAssistant,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Removing the entry shows its relays and takes its device and entity away."""
    assert device_registry.async_get_device_by_identifier(
        (DOMAIN, cover.entry_id), cover.entry_id
    )
    assert await hass.config_entries.async_remove(cover.entry_id)
    await hass.async_block_till_done()

    for relay in (OPEN_RELAY, CLOSE_RELAY):
        assert _hidden_by(entity_registry, relay) is None
    assert entity_registry.async_get(COVER) is None
    assert hass.states.get(COVER) is None
    assert not device_registry.async_get_device_by_identifier(
        (DOMAIN, cover.entry_id), cover.entry_id
    )


async def test_unload_entry(hass: HomeAssistant, flow_cover: MockConfigEntry) -> None:
    """The entry unloads cleanly, its entity goes unavailable and the relays stay hidden."""
    assert await hass.config_entries.async_unload(flow_cover.entry_id)
    await hass.async_block_till_done()
    assert flow_cover.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(COVER).state == STATE_UNAVAILABLE
    registry = er.async_get(hass)
    assert _hidden_by(registry, OPEN_RELAY) is er.RegistryEntryHider.INTEGRATION


@pytest.mark.usefixtures("relays")
async def test_cover_has_its_own_device(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The cover gets a device of its own, not the device of the relays."""
    relay_owner = MockConfigEntry(domain="fake_relay")
    relay_owner.add_to_hass(hass)
    relay_device = device_registry.async_get_or_create(
        config_entry_id=relay_owner.entry_id,
        identifiers={("fake_relay", "board-1")},
        name="Relay board",
    )
    for relay in (OPEN_RELAY, CLOSE_RELAY):
        entity_registry.async_update_entity(relay, device_id=relay_device.id)

    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, mock_config_entry.entry_id), mock_config_entry.entry_id
    )
    assert device is not None
    assert device.name == "Living room"
    assert device.config_entry_id == mock_config_entry.entry_id
    registry_entry = entity_registry.async_get(COVER)
    assert registry_entry is not None
    assert registry_entry.device_id == device.id
    assert registry_entry.device_id != relay_device.id
    assert hass.states.get(COVER).attributes["friendly_name"] == "Living room"
    # The relay device is untouched
    assert device_registry.async_get(relay_device.id).config_entry_id == (
        relay_owner.entry_id
    )


async def test_renamed_relay_is_followed(
    hass: HomeAssistant, flow_cover: MockConfigEntry, entity_registry: er.EntityRegistry
) -> None:
    """Renaming a relay updates the options and reloads; it stays hidden and works."""
    entity_registry.async_update_entity(CLOSE_RELAY, new_entity_id="switch.down_new")
    await hass.async_block_till_done()

    assert flow_cover.options[CONF_CLOSE_RELAY] == "switch.down_new"
    assert flow_cover.runtime_data.close_relay == "switch.down_new"
    assert flow_cover.state is ConfigEntryState.LOADED
    assert _hidden_by(entity_registry, "switch.down_new") is (
        er.RegistryEntryHider.INTEGRATION
    )
    assert hass.states.get(COVER).state != STATE_UNAVAILABLE


async def test_removed_relay_keeps_cover_on_its_device(
    hass: HomeAssistant,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Removing a relay leaves the cover on its own device, available to be stopped."""
    entity_registry.async_remove(OPEN_RELAY)
    await hass.async_block_till_done()

    assert hass.states.get(COVER).state != STATE_UNAVAILABLE
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, cover.entry_id), cover.entry_id
    )
    assert device is not None
    registry_entry = entity_registry.async_get(COVER)
    assert registry_entry is not None
    assert registry_entry.device_id == device.id


async def test_cover_unavailable_while_both_relays_unavailable(
    hass: HomeAssistant, cover: MockConfigEntry
) -> None:
    """The cover is unavailable only when no relay answers, so it can be stopped."""
    hass.states.async_set(OPEN_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state != STATE_UNAVAILABLE

    hass.states.async_set(CLOSE_RELAY, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state == STATE_UNAVAILABLE

    hass.states.async_set(OPEN_RELAY, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state != STATE_UNAVAILABLE


async def test_cover_waits_for_relays_at_startup(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """At startup the relays may appear after the cover: it becomes available then."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state == STATE_UNAVAILABLE

    hass.states.async_set(OPEN_RELAY, STATE_OFF)
    hass.states.async_set(CLOSE_RELAY, STATE_OFF)
    await hass.async_block_till_done()
    assert hass.states.get(COVER).state != STATE_UNAVAILABLE


async def test_removed_relay_raises_a_repair_issue(
    hass: HomeAssistant,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A removed relay raises a repair issue; choosing another relay drops it."""
    issue_id = f"relay_removed_{cover.entry_id}_{CONF_OPEN_RELAY}"
    entity_registry.async_remove(OPEN_RELAY)
    await hass.async_block_till_done()
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {
        "cover": "Living room",
        "relay": OPEN_RELAY,
    }

    # A restart keeps it while the relay is still missing
    assert await hass.config_entries.async_reload(cover.entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    entity_registry.async_get_or_create(Platform.SWITCH, "test", "spare")
    spare = entity_registry.async_get_entity_id(Platform.SWITCH, "test", "spare")
    result = await _configure_options(hass, cover, **{CONF_OPEN_RELAY: spare})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_removing_the_entry_drops_its_repair_issue(
    hass: HomeAssistant,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """Removing the entry drops the repair issue of its removed relay."""
    entity_registry.async_remove(CLOSE_RELAY)
    await hass.async_block_till_done()
    assert len(issue_registry.issues) == 1
    assert await hass.config_entries.async_remove(cover.entry_id)
    await hass.async_block_till_done()
    assert not issue_registry.issues


async def test_issue_dropped_when_the_relay_comes_back(
    hass: HomeAssistant,
    cover: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """The removed relay is added back: the issue goes away at the next setup.

    Not as soon as the relay has a state again: the entry checks its relays when
    it is set up, after a reload or a restart.
    """
    issue_id = f"relay_removed_{cover.entry_id}_{CONF_CLOSE_RELAY}"
    registry_entry = entity_registry.async_get(CLOSE_RELAY)
    assert registry_entry is not None
    entity_registry.async_remove(CLOSE_RELAY)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    entity_registry.async_get_or_create(
        Platform.SWITCH,
        registry_entry.platform,
        registry_entry.unique_id,
        suggested_object_id="down",
    )
    hass.states.async_set(CLOSE_RELAY, STATE_OFF)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None
    assert await hass.config_entries.async_reload(cover.entry_id)
    await hass.async_block_till_done()
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
    assert cover.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    ("changes", "switched_off"),
    [
        pytest.param({CONF_OPEN_RELAY: "switch.spare"}, [OPEN_RELAY], id="replaced"),
        pytest.param(
            {CONF_OPEN_RELAY: CLOSE_RELAY, CONF_CLOSE_RELAY: OPEN_RELAY},
            [OPEN_RELAY],
            id="swapped",
        ),
        pytest.param({CONF_OPENING_TIME: 25}, [], id="same_relays"),
    ],
)
async def test_relay_leaving_its_role_is_switched_off(
    hass: HomeAssistant,
    cover: MockConfigEntry,
    changes: dict[str, Any],
    switched_off: list[str],
) -> None:
    """Options saved while opening: a relay that leaves its role goes off."""
    await hass.services.async_call(
        Platform.COVER, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: COVER}, blocking=True
    )
    await hass.async_block_till_done()
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)
    result = await _configure_options(hass, cover, **changes)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [
        e.data["service_data"][ATTR_ENTITY_ID]
        for e in calls
        if e.data["service"] == SERVICE_TURN_OFF
    ] == switched_off


async def test_relay_hidden_by_another_integration_is_shown_too(
    hass: HomeAssistant, relays: None, entity_registry: er.EntityRegistry
) -> None:
    """As the helpers of the core: a relay hidden by any integration is shown again.

    The registry only says that an integration hid it, not which one: telling them apart
    would need a mark of our own in the entity options.
    """
    entity_registry.async_update_entity(
        OPEN_RELAY, hidden_by=er.RegistryEntryHider.INTEGRATION
    )
    entry = await _create_with_flow(hass)
    await _configure_options(hass, entry, **{CONF_HIDE_RELAYS: False})
    assert _hidden_by(entity_registry, OPEN_RELAY) is None
    assert _hidden_by(entity_registry, CLOSE_RELAY) is None

    await _configure_options(hass, entry, **{CONF_HIDE_RELAYS: True})
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _hidden_by(entity_registry, OPEN_RELAY) is None
    assert _hidden_by(entity_registry, CLOSE_RELAY) is None
