"""Unit tests for the OpenEMS integration helpers.

These tests exercise the OpenEMS channel/property/component logic without
network calls by using small dummy objects.
"""

from datetime import time
from unittest.mock import AsyncMock, MagicMock

import jsonrpc_base.jsonrpc
import pytest
from yarl import URL

from openems_client import openems
from openems_client.const import SLASH_ESC
from openems_client.exceptions import OpenEMSConnectionError, OpenEMSError
from openems_client.helpers import wrap_jsonrpc
from openems_client.helpers_openems import NumberExpression
from openems_client.model import ChannelKind


def _make_backend():
    """Return a minimal mock backend for OpenEMSEdge construction."""
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/openems-backend-ui")
    backend.connection.notify_data_received = MagicMock()
    return backend


def _make_component(name="ctrlEvcs1"):
    """Return a minimal mock component for property construction."""
    comp = MagicMock()
    comp.name = name
    comp.properties = {}
    comp.edge.hostname = "host"
    comp.edge.id = "edgeid"
    return comp


def make_channel_json(id_, type_, unit="unit", accessMode="RO"):
    """Create a simple channel JSON dict for tests."""
    return {"id": id_, "type": type_, "unit": unit, "accessMode": accessMode}


def test_wrap_jsonrpc() -> None:
    """Test that wrap_jsonrpc produces a valid JSON-RPC envelope."""
    env = wrap_jsonrpc("testMethod", a=1)
    assert env["method"] == "testMethod"
    assert "id" in env
    assert env["params"]["a"] == 1


async def test_edge_dispatch_currentData() -> None:
    """Test that currentData updates current_channel_data on the edge."""
    backend = _make_backend()
    component_config = {"_host": {"Hostname": "h1"}}
    edge = openems.OpenEMSEdge(backend, "edge-1", component_config)
    try:
        edge._registered_handlers["a/b"] = set()
        edge.currentData({"a/b": 1})
        assert edge.current_channel_data == {"a/b": 1}
    finally:
        await edge.stop()


async def test_edge_execute_system_restart() -> None:
    """executeSystemRestart is sent as HARD restart via the _host component."""
    backend = _make_backend()
    backend.connection.rpc_server.edgeRpc = AsyncMock()
    edge = openems.OpenEMSEdge(backend, "edge-1", {"_host": {"Hostname": "h1"}})
    try:
        await edge.execute_system_restart()
    finally:
        await edge.stop()

    backend.connection.rpc_server.edgeRpc.assert_awaited_once()
    kwargs = backend.connection.rpc_server.edgeRpc.await_args.kwargs
    assert kwargs["edgeId"] == "edge-1"
    edge_call = kwargs["payload"]
    assert edge_call["method"] == "componentJsonApi"
    assert edge_call["params"]["componentId"] == "_host"
    restart_call = edge_call["params"]["payload"]
    assert restart_call["method"] == "executeSystemRestart"
    assert restart_call["params"] == {"type": "HARD"}


async def test_component_boolean_property() -> None:
    """Test that a BOOLEAN _Property channel is created and handles data."""
    backend = _make_backend()
    comp_json = {
        "_PropertyAlias": "alias",
        "properties": {},
        "channels": [
            make_channel_json("_PropertyEnabledCharging", "BOOLEAN"),
            make_channel_json("SomeSensor", "INTEGER"),
        ],
    }
    component_config = {"_host": {"Hostname": "h1"}, "comp1": comp_json}
    edge = openems.OpenEMSEdge(backend, "edge-1", component_config)
    try:
        assert "comp1" in edge.components
        comp = edge.components["comp1"]
        assert any(
            p.name == "_PropertyEnabledCharging"
            for p in comp.channels_of(ChannelKind.SWITCH)
        )

        prop = comp.channels_of(ChannelKind.SWITCH)[0]
        called = False

        def cb():
            nonlocal called
            called = True

        prop.add_listener(cb)
        edge.currentData({"comp1/_PropertyEnabledCharging": 1})
        assert prop.current_value is True
        assert called
        assert "/_PropertyEnabledCharging" in prop.unique_id
    finally:
        await edge.stop()


def test_enum_property_behavior() -> None:
    """Test enum property selection and update."""
    comp = _make_component()
    enum_json = {"id": "_PropertyChargeMode", "type": "STRING", "unit": "u"}
    options = ["EXCESS_POWER", "MANUAL", "OFF"]
    prop = openems.OpenEMSEnumProperty(
        component=comp, channel_json=enum_json, options=options
    )
    prop.handle_data_update("_PropertyChargeMode", "EXCESS_POWER")
    assert prop.value == "excess_power"
    # unknown option is ignored
    prop.handle_data_update("_PropertyChargeMode", "UNKNOWN")
    assert prop.value is None


def test_time_property_behavior() -> None:
    """Test time property parsing."""
    comp = _make_component()
    time_json = {"id": "_PropertyManualTargetTime", "type": "STRING", "unit": "u"}
    prop = openems.OpenEMSTimeProperty(component=comp, channel_json=time_json)
    prop.handle_data_update("_PropertyManualTargetTime", "12:34")
    assert prop.value == time(12, 34)
    # invalid time string
    prop.handle_data_update("_PropertyManualTargetTime", "nope")
    assert prop.value is None


def test_number_property_multiplier_and_limits() -> None:
    """Test number property applies multiplier on data update."""
    comp = _make_component()
    num_json = {"id": "_PropertyEnergySessionLimit", "type": "INTEGER", "unit": "W"}
    prop = openems.OpenEMSNumberProperty(component=comp, channel_json=num_json)
    prop.set_multiplier_def("2")
    prop.set_limit_def({"lower": "1", "upper": "100"})

    assert prop.multiplier == 2.0
    assert prop.lower_limit >= 0
    assert prop.upper_limit >= prop.lower_limit

    prop.handle_data_update("comp/_PropertyEnergySessionLimit", 10)
    assert prop.current_value == 20


def _make_component_with_edge(name: str = "ctrlEvcs1"):
    """Return a mock component with a real DummyEdge supporting register/unregister."""
    comp = MagicMock()
    comp.name = name
    comp.json_properties = {}

    class DummyEdge:
        def __init__(self) -> None:
            self.hostname = "h"
            self.id = "edge"
            self._registered_handlers: dict = {}

        def register_channel(self, channel_names, handler):
            for ch_name in channel_names:
                self._registered_handlers.setdefault(ch_name, set()).add(handler)

        def unregister_channel(self, handler):
            for ch_name, handlers in list(self._registered_handlers.items()):
                handlers.discard(handler)

    comp.edge = DummyEdge()
    return comp


def test_channel_listeners() -> None:
    """All listeners are notified; the channel is subscribed while it has listeners."""
    comp = _make_component_with_edge()
    chan_json = {"id": "SomeSensor", "type": "INTEGER", "unit": "u"}
    chan = openems.OpenEMSChannel(component=comp, channel_json=chan_json)
    calls: list[str] = []

    remove_first = chan.add_listener(lambda: calls.append("first"))
    remove_second = chan.add_listener(lambda: calls.append("second"))
    handlers = comp.edge._registered_handlers
    assert chan in handlers["ctrlEvcs1/SomeSensor"]

    chan.handle_data_update("ctrlEvcs1/SomeSensor", 5)
    assert calls == ["first", "second"]

    remove_first()
    assert chan in handlers["ctrlEvcs1/SomeSensor"]
    chan.handle_data_update("ctrlEvcs1/SomeSensor", 6)
    assert calls == ["first", "second", "second"]

    remove_second()
    assert chan not in handlers["ctrlEvcs1/SomeSensor"]


async def test_set_unavailable_clears_values() -> None:
    """Test that set_unavailable calls handle_data_update(None) for active channels."""
    backend = _make_backend()
    component_config = {"_host": {"Hostname": "h1"}}
    edge = openems.OpenEMSEdge(backend, "e1", component_config)
    try:
        comp = _make_component_with_edge()
        chan_json = {"id": "S", "type": "INTEGER", "unit": "u"}
        ch = openems.OpenEMSChannel(component=comp, channel_json=chan_json)

        # Register the channel directly in the edge handler map
        edge._registered_handlers["c1/S"] = {ch}
        edge.current_channel_data = {"c1/S": 5}

        edge.set_unavailable()
        assert ch.current_value is None
    finally:
        await edge.stop()


def test_number_property_with_template_references() -> None:
    """Test OpenEMSNumberProperty with template references to other channels."""
    comp = _make_component_with_edge("evcs1")
    # needed for $evcs.id resolution
    comp.json_properties["evcs.id"] = comp.name

    num_json = {"id": "_PropertyForceChargeMinPower", "type": "INTEGER", "unit": "W"}
    num_prop = openems.OpenEMSNumberProperty(component=comp, channel_json=num_json)

    num_prop.set_multiplier_def("{{$evcs.id/Phases}}")
    num_prop.set_limit_def({"lower": "1", "upper": "100"})

    ref_key = "evcs1" + SLASH_ESC + "Phases"
    assert ref_key in num_prop.reference_channels

    num_prop.reference_channels[ref_key] = 3
    num_prop._update_config()
    assert num_prop.multiplier == 3.0


def _evcs_component(evcs_id: str = "evcs0") -> MagicMock:
    comp = _make_component("ctrlEvcs0")
    comp.json_properties = {"evcs.id": evcs_id, "ess.id": "ess0"}
    return comp


@pytest.mark.parametrize(
    ("expr", "values", "expected"),
    [
        pytest.param("100000", {}, 100000.0, id="constant"),
        pytest.param(
            "{{$ess.id/MaxApparentPower}}",
            {"ess0" + SLASH_ESC + "MaxApparentPower": 10000},
            10000.0,
            id="reference",
        ),
        pytest.param(
            "0-{{$ess.id/MaxApparentPower}}",
            {"ess0" + SLASH_ESC + "MaxApparentPower": 10000},
            -10000.0,
            id="negated_reference",
        ),
        pytest.param(
            "{{$evcs.id/MaximumHardwarePower}} / {{$evcs.id/Phases}}",
            {
                "evcs0" + SLASH_ESC + "MaximumHardwarePower": 22000,
                "evcs0" + SLASH_ESC + "Phases": 2,
            },
            11000.0,
            id="division",
        ),
        pytest.param(
            "{{$evcs.id/MaximumHardwarePower}} / {{$evcs.id/Phases}}",
            {
                "evcs0" + SLASH_ESC + "MaximumHardwarePower": 22000,
                "evcs0" + SLASH_ESC + "Phases": 0,
            },
            None,
            id="division_by_zero",
        ),
        pytest.param(
            "0-{{$ess.id/MaxApparentPower}}",
            {"ess0" + SLASH_ESC + "MaxApparentPower": None},
            None,
            id="value_missing",
        ),
        pytest.param(
            "0-{{$ess.id/MaxApparentPower}}",
            {"ess0" + SLASH_ESC + "MaxApparentPower": "unknown"},
            None,
            id="value_not_a_number",
        ),
        pytest.param("0-{{$ess.id/MaxApparentPower}}", {}, None, id="value_unknown"),
    ],
)
def test_number_expression_evaluate(
    expr: str, values: dict[str, openems.BackendValue], expected: float | None
) -> None:
    """Expressions of the config evaluate to numbers, invalid values to None."""
    assert NumberExpression(expr, _evcs_component()).evaluate(values) == expected


def test_number_expression_linked_channels_are_unique() -> None:
    """A channel referenced twice is linked once."""
    expression = NumberExpression(
        "{{$evcs.id/Phases}} * {{$evcs.id/Phases}}", _evcs_component()
    )
    assert expression.linked_channels == ["evcs0" + SLASH_ESC + "Phases"]
    assert expression.evaluate({"evcs0" + SLASH_ESC + "Phases": 3}) == 9.0


def test_number_expression_edge_values_are_no_code() -> None:
    """A property value from the edge is only a channel key, it is never evaluated."""
    payload = "0}}{{ cycler.__init__.__globals__.os.getcwd() }}{{ 0 if "
    comp = _evcs_component()
    comp.json_properties["ess.id"] = payload
    expression = NumberExpression("0-{{$ess.id/MaxApparentPower}}", comp)

    linked_channel = payload + SLASH_ESC + "MaxApparentPower"
    assert expression.linked_channels == [linked_channel]
    assert expression.evaluate({linked_channel: 100}) == -100.0


def test_number_expression_constant_rejects_non_numbers() -> None:
    """An expression without references must be a number."""
    with pytest.raises(ValueError, match="Invalid number expression"):
        NumberExpression("'abc'", _evcs_component()).constant()


def test_number_property_limits_fall_back_on_division_by_zero() -> None:
    """Limits dividing by a channel value of 0 fall back to the defaults."""
    comp = _make_component_with_edge("ctrlEvcs0")
    comp.json_properties["evcs.id"] = "evcs0"
    num_json = {"id": "_PropertyForceChargeMinPower", "type": "INTEGER", "unit": "W"}
    num_prop = openems.OpenEMSNumberProperty(component=comp, channel_json=num_json)
    num_prop.set_limit_def(
        {
            "lower": "{{$evcs.id/MinimumHardwarePower}} / {{$evcs.id/Phases}}",
            "upper": "{{$evcs.id/MaximumHardwarePower}} / {{$evcs.id/Phases}}",
        }
    )

    num_prop.handle_data_update("evcs0/MinimumHardwarePower", 4200)
    num_prop.handle_data_update("evcs0/MaximumHardwarePower", 22000)
    num_prop.handle_data_update("evcs0/Phases", 0)

    assert (num_prop.lower_limit, num_prop.upper_limit) == (0, 100000)


async def test_entity_lifecycle_and_unique_id(dummy_backend) -> None:
    """Test entities are prepared and unique_ids are stable."""
    comp = {
        "_PropertyAlias": "a",
        "properties": {},
        "channels": [
            {"id": "ch1", "type": "INTEGER", "unit": "u"},
            {"id": "ch2", "type": "DOUBLE", "unit": "kWh"},
        ],
    }
    component_config = {"_host": {"Hostname": "edge1"}, "c1": comp}
    edge = openems.OpenEMSEdge(dummy_backend, "edge1", component_config)
    try:
        comp_obj = edge.components["c1"]
        assert len(comp_obj.channels_of(ChannelKind.SENSOR)) >= 1
        for s in comp_obj.channels_of(ChannelKind.SENSOR):
            assert s.unique_id.startswith("edge1")
    finally:
        await edge.stop()


async def test_register_channel_skips_channels_without_read_access(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only channels with READ access are registered for subscriptions."""
    component_config = {
        "_host": {"Hostname": "h1"},
        "comp1": {
            "_PropertyAlias": "component",
            "properties": {},
            "channels": [
                {
                    "id": "ReadOnly",
                    "type": "INTEGER",
                    "unit": "u",
                    "accessMode": "RO",
                },
                {
                    "id": "ReadWrite",
                    "type": "INTEGER",
                    "unit": "u",
                    "accessMode": "RW",
                },
                {
                    "id": "WriteOnly",
                    "type": "INTEGER",
                    "unit": "u",
                    "accessMode": "WO",
                },
                {"id": "Unknown", "type": "INTEGER", "unit": "u"},
            ],
        },
    }
    edge = openems.OpenEMSEdge(_make_backend(), "edge-1", component_config)
    caplog.set_level("INFO")

    try:
        for channel in edge.components["comp1"].channels:
            edge.register_channel({f"comp1/{channel.name}"}, channel)

        assert set(edge.registered_channels) == {
            "comp1/ReadOnly",
            "comp1/ReadWrite",
        }
        assert "Skipping subscription for channel comp1/WriteOnly" in caplog.text
        assert "Skipping subscription for channel comp1/Unknown" in caplog.text
    finally:
        await edge.stop()


def _make_evcs_edge():
    """Return an edge with a ctrlEvcs0 component and a mocked update_config."""
    component_config = {
        "_host": {"Hostname": "h1"},
        "ctrlEvcs0": {
            "_PropertyAlias": "EVCS",
            "properties": {},
            "channels": [
                make_channel_json("_PropertyEnabledCharging", "BOOLEAN"),
                make_channel_json("_PropertyChargeMode", "STRING"),
            ],
        },
    }
    edge = openems.OpenEMSEdge(_make_backend(), "edge-1", component_config)
    comp = edge.components["ctrlEvcs0"]
    comp.update_config = AsyncMock()
    return edge, comp


async def test_update_properties_adds_update_group_members() -> None:
    """Switching charging on adds the current charge mode to the same request."""
    edge, comp = _make_evcs_edge()
    try:
        comp.get_property("chargeMode").handle_data_update("", "EXCESS_POWER")
        await comp.get_property("enabledCharging").update_value(True)
        comp.update_config.assert_awaited_once_with(
            [("enabledCharging", True), ("chargeMode", "EXCESS_POWER")]
        )
    finally:
        await edge.stop()


async def test_update_properties_respects_when_condition() -> None:
    """Switching charging off does not add the charge mode (rule has when: true)."""
    edge, comp = _make_evcs_edge()
    try:
        await comp.get_property("enabledCharging").update_value(False)
        comp.update_config.assert_awaited_once_with([("enabledCharging", False)])
    finally:
        await edge.stop()


async def test_update_properties_explicit_values_win() -> None:
    """Explicitly given values are not overwritten by current values of group members."""
    edge, comp = _make_evcs_edge()
    try:
        enabled = comp.get_property("_PropertyEnabledCharging")
        mode = comp.get_property("_PropertyChargeMode")
        enabled.handle_data_update("", 0)
        await comp.update_properties({enabled: True, mode: "FORCE_CHARGE"})
        comp.update_config.assert_awaited_once_with(
            [("enabledCharging", True), ("chargeMode", "FORCE_CHARGE")]
        )
    finally:
        await edge.stop()


async def test_get_property_by_channel_or_config_name() -> None:
    """Properties can be looked up by channel name and config name."""
    edge, comp = _make_evcs_edge()
    try:
        prop = comp.get_property("chargeMode")
        assert prop is not None
        assert comp.get_property("_PropertyChargeMode") is prop
        assert prop.config_name == "chargeMode"
        assert comp.get_property("unknown") is None
    finally:
        await edge.stop()


def test_to_backend_value_conversions() -> None:
    """Each property type converts HA values to backend values."""
    comp = _make_component()
    enum_prop = openems.OpenEMSEnumProperty(
        comp, make_channel_json("_PropertyChargeMode", "STRING"), ["FORCE_CHARGE"]
    )
    assert enum_prop.to_backend_value("force_charge") == "FORCE_CHARGE"
    assert enum_prop.to_backend_value("FORCE_CHARGE") == "FORCE_CHARGE"
    with pytest.raises(ValueError):
        enum_prop.to_backend_value("off")

    bool_prop = openems.OpenEMSBooleanProperty(
        comp, make_channel_json("_PropertyEnabledCharging", "BOOLEAN")
    )
    assert bool_prop.to_backend_value(True) is True
    assert bool_prop.to_backend_value("off") is False
    assert bool_prop.to_backend_value(1) is True
    with pytest.raises(ValueError):
        bool_prop.to_backend_value(2)

    num_prop = openems.OpenEMSNumberProperty(
        comp, make_channel_json("_PropertyForceChargeMinPower", "INTEGER")
    )
    num_prop.multiplier = 3
    assert num_prop.to_backend_value("4200") == 1400
    for invalid in ("abc", None, True):
        with pytest.raises(ValueError):
            num_prop.to_backend_value(invalid)

    time_prop = openems.OpenEMSTimeProperty(
        comp, make_channel_json("_PropertyManualTargetTime", "STRING")
    )
    assert time_prop.to_backend_value(time(7, 5)) == "07:05"
    assert time_prop.to_backend_value("07:05:00") == "07:05"
    for invalid in ("nope", 7):
        with pytest.raises(ValueError):
            time_prop.to_backend_value(invalid)


async def test_heat_pump_sg_ready_properties() -> None:
    """SG-Ready heat pump properties become selects and numbers (issue #61)."""
    component_config = {
        "_host": {"Hostname": "h1"},
        "ctrlIoHeatPump0": {
            "_PropertyAlias": "Heat pump",
            "properties": {},
            # enum properties are reported by OpenEMS as STRING without options
            "channels": [
                make_channel_json("_PropertyMode", "STRING", ""),
                make_channel_json("_PropertyManualState", "STRING", ""),
                make_channel_json("_PropertyAutomaticForceOnSoc", "INTEGER", ""),
                make_channel_json("_PropertyAutomaticLockSoc", "INTEGER", ""),
                make_channel_json("_PropertyAutomaticLockCtrlEnabled", "BOOLEAN", ""),
                make_channel_json("_PropertyAutomaticLockGridBuyPower", "INTEGER", ""),
                make_channel_json("_PropertyMinimumSwitchingTime", "INTEGER", ""),
                {
                    "id": "Status",
                    "type": "INTEGER",
                    "category": "ENUM",
                    "unit": "",
                    "options": {
                        "Undefined": -1,
                        "Blocks everything till an internal maximum time of default two hours": 0,
                        "Default energy-efficient operation": 1,
                        "Recommendation to use more available power": 2,
                        "Force all possible consumption of the heat pump": 3,
                    },
                },
            ],
        },
    }
    edge = openems.OpenEMSEdge(_make_backend(), "edge-1", component_config)
    try:
        comp = edge.components["ctrlIoHeatPump0"]
        comp.update_config = AsyncMock()
        selects = {
            p.name: p
            for p in comp.channels_of(ChannelKind.SELECT, openems.OpenEMSEnumProperty)
        }
        assert selects["_PropertyMode"].property_options == ["MANUAL", "AUTOMATIC"]
        assert selects["_PropertyManualState"].property_options == [
            "LOCK",
            "REGULAR",
            "RECOMMENDATION",
            "FORCE_ON",
        ]
        numbers = {
            p.name: p
            for p in comp.channels_of(ChannelKind.NUMBER, openems.OpenEMSNumberProperty)
        }
        for name in ("_PropertyAutomaticForceOnSoc", "_PropertyAutomaticLockSoc"):
            assert (numbers[name].lower_limit, numbers[name].upper_limit) == (0, 100)
            assert numbers[name].step == 1
        # thresholds: fixed step 1 instead of the calculated 1000 / 100
        threshold_limits = {
            "_PropertyAutomaticLockGridBuyPower": (0, 100000),
            "_PropertyMinimumSwitchingTime": (0, 3600),
        }
        for name, limits in threshold_limits.items():
            assert (numbers[name].lower_limit, numbers[name].upper_limit) == limits
            assert numbers[name].step == 1

        # no update group: each property is sent individually
        await selects["_PropertyManualState"].update_value("RECOMMENDATION")
        comp.update_config.assert_awaited_once_with([("manualState", "RECOMMENDATION")])

    finally:
        await edge.stop()


async def test_edge_request_errors_are_converted() -> None:
    """Errors of edge requests are raised as OpenEMS client errors."""
    backend = _make_backend()
    backend.connection.rpc_server.edgeRpc = AsyncMock(
        side_effect=jsonrpc_base.jsonrpc.ProtocolError(500, "Internal error")
    )
    edge = openems.OpenEMSEdge(backend, "edge-1", {"_host": {"Hostname": "h1"}})
    try:
        with pytest.raises(OpenEMSError):
            await edge.get_system_update_state()
        with pytest.raises(OpenEMSError):
            await edge.execute_system_restart()
    finally:
        await edge.stop()


async def test_update_config_raises_when_not_connected() -> None:
    """Writing component properties without a connection raises OpenEMSConnectionError."""
    backend = _make_backend()
    backend.connection.rpc_server.connected = False
    component_config = {
        "_host": {"Hostname": "h1"},
        "ctrlEvcs0": {
            "_PropertyAlias": "EVCS",
            "properties": {},
            "channels": [make_channel_json("_PropertyEnabledCharging", "BOOLEAN")],
        },
    }
    edge = openems.OpenEMSEdge(backend, "edge-1", component_config)
    try:
        with pytest.raises(OpenEMSConnectionError):
            await edge.components["ctrlEvcs0"].update_config(
                [("enabledCharging", True)]
            )
    finally:
        await edge.stop()


async def test_edge_starts_background_tasks_only_on_start() -> None:
    """Constructing an edge starts no tasks, start() starts and stop() ends them."""
    edge = openems.OpenEMSEdge(_make_backend(), "edge-1", {"_host": {"Hostname": "h1"}})
    edge.set_forward_interval(5)
    assert edge._channel_subscription_updater._fetch_task is None
    assert edge._channel_data_forwarder is None

    edge.start()
    fetch_task = edge._channel_subscription_updater._fetch_task
    forwarder = edge._channel_data_forwarder
    assert fetch_task is not None
    assert forwarder is not None

    await edge.stop()

    assert fetch_task.done()
    assert forwarder.done()
    assert edge._channel_subscription_updater._fetch_task is None
    assert edge._channel_data_forwarder is None


async def test_backend_start_passes_auth_failed_callback() -> None:
    """start() enables the reconnects with the callback for rejected logins."""
    connection = MagicMock()
    backend = openems.OpenEMSBackend(
        connection, "edge-1", False, {"_host": {"Hostname": "h1"}}
    )
    auth_failed_callback = MagicMock()

    backend.start(auth_failed_callback)

    connection.enable_reconnect.assert_called_once_with(
        backend.the_edge.set_unavailable, auth_failed_callback
    )
    await backend.the_edge.stop()


async def test_edge_restarts_forwarder_on_interval_change() -> None:
    """Changing the forward interval of a started edge replaces the forwarder task."""
    edge = openems.OpenEMSEdge(_make_backend(), "edge-1", {"_host": {"Hostname": "h1"}})
    edge.start()
    try:
        assert edge._channel_data_forwarder is None

        edge.set_forward_interval(5)
        first_forwarder = edge._channel_data_forwarder
        assert first_forwarder is not None

        edge.set_forward_interval(10)
        assert edge._channel_data_forwarder is not first_forwarder

        edge.set_forward_interval(0)
        assert edge._channel_data_forwarder is None
    finally:
        await edge.stop()


async def test_write_via_rest_posts_value_with_injected_session() -> None:
    """A one-shot REST write posts the value with basic auth via the injected session."""
    backend = _make_backend()
    backend.connection.username = "user"
    backend.connection.password = "pass"
    response = MagicMock(status=200)
    post_context = MagicMock()
    post_context.__aenter__ = AsyncMock(return_value=response)
    post_context.__aexit__ = AsyncMock(return_value=None)
    backend.connection.session.post = MagicMock(return_value=post_context)
    component_config = {
        "_host": {"Hostname": "h1"},
        "ctrlApiRest0": {
            "factoryId": "Controller.Api.Rest.ReadWrite",
            "properties": {"port": 8084},
        },
        "meter0": {
            "properties": {},
            "channels": [
                make_channel_json("SetActivePowerEquals", "INTEGER", accessMode="WO")
            ],
        },
    }
    edge = openems.OpenEMSEdge(backend, "edge-1", component_config)
    channel = edge.components["meter0"].channels_of(
        ChannelKind.WRITE_ONLY, openems.OpenEMSChannel
    )[0]

    await channel.write_via_rest(1500)

    url = backend.connection.session.post.call_args.args[0]
    kwargs = backend.connection.session.post.call_args.kwargs
    assert str(url) == "http://localhost:8084/rest/channel/meter0/SetActivePowerEquals"
    assert kwargs["json"] == {"value": 1500}
    assert kwargs["headers"] == {"Authorization": "Basic dXNlcjpwYXNz"}
