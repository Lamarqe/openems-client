"""Tests for the channel classification of the OpenEMS client."""

from datetime import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from yarl import URL

from openems_client.helpers import to_snake_case
from openems_client.model import (
    ChannelKind,
    Quantity,
    Severity,
    UnitInfo,
    channel_key,
    display_name,
    severity,
    unit_info,
)
from openems_client.openems import (
    CONFIG,
    OpenEMSBooleanProperty,
    OpenEMSChannel,
    OpenEMSEdge,
    OpenEMSEnumProperty,
    OpenEMSNumberProperty,
    OpenEMSTimeProperty,
)


@pytest.mark.parametrize(
    ("unit", "key", "expected"),
    [
        pytest.param(
            "",
            "state",
            UnitInfo(unit=None, quantity=None, cumulative=False),
            id="no_unit",
        ),
        pytest.param(
            "foo",
            "x",
            UnitInfo(unit="foo", quantity=None, cumulative=False),
            id="unknown_unit",
        ),
        pytest.param(
            "W",
            "active_power",
            UnitInfo(unit="W", quantity=Quantity.POWER, cumulative=False),
            id="power",
        ),
        pytest.param(
            "Wh",
            "capacity",
            UnitInfo(unit="Wh", quantity=Quantity.ENERGY, cumulative=False),
            id="energy",
        ),
        pytest.param(
            "Wh_Σ",
            "active_energy",
            UnitInfo(unit="Wh", quantity=Quantity.ENERGY, cumulative=True),
            id="energy_counter",
        ),
        pytest.param(
            "sec_Σ",
            "uptime",
            UnitInfo(unit="s", quantity=Quantity.DURATION, cumulative=True),
            id="duration_counter",
        ),
        pytest.param(
            "sec",
            "delay",
            UnitInfo(unit="s", quantity=Quantity.DURATION, cumulative=False),
            id="seconds",
        ),
        pytest.param(
            "C",
            "temperature",
            UnitInfo(unit="°C", quantity=Quantity.TEMPERATURE, cumulative=False),
            id="celsius",
        ),
        pytest.param(
            "°C",
            "cpu_temperature",
            UnitInfo(unit="°C", quantity=Quantity.TEMPERATURE, cumulative=False),
            id="degree_celsius",
        ),
        pytest.param(
            "°dC",
            "temperature_sensor1",
            UnitInfo(
                unit="°C", quantity=Quantity.TEMPERATURE, cumulative=False, divisor=10
            ),
            id="decidegree_celsius",
        ),
        pytest.param(
            "%",
            "soc",
            UnitInfo(unit="%", quantity=Quantity.STATE_OF_CHARGE, cumulative=False),
            id="soc",
        ),
        pytest.param(
            "%",
            "ess_soc",
            UnitInfo(unit="%", quantity=Quantity.STATE_OF_CHARGE, cumulative=False),
            id="ess_soc",
        ),
        pytest.param(
            "%",
            "efficiency",
            UnitInfo(unit="%", quantity=Quantity.PERCENTAGE, cumulative=False),
            id="percentage",
        ),
        pytest.param(
            "%",
            "associated",
            UnitInfo(unit="%", quantity=Quantity.PERCENTAGE, cumulative=False),
            id="soc_suffix_only_as_word",
        ),
    ],
)
def test_unit_info(unit: str, key: str, expected: UnitInfo) -> None:
    """Test the unit normalisation."""
    assert unit_info(unit, key) == expected


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("_PropertyChargeMode", "charge_mode"),
        ("EssSoc", "ess_soc"),
        ("ActualEnergy", "actual_energy"),
        ("_PropertyIsReserveSocEnabled", "is_reserve_soc_enabled"),
        (
            "Tower0Module1CellVoltageDifference",
            "tower0_module1_cell_voltage_difference",
        ),
        ("DcDischargePower", "dc_discharge_power"),
    ],
)
def test_channel_key(name: str, key: str) -> None:
    """Test the snake_case keys of channel names."""
    assert channel_key(name) == key


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("ActivePowerL1", "Active power L1", id="phase"),
        pytest.param("DcChargeEnergy", "DC charge energy", id="acronym"),
        pytest.param("EssMaxApparentPower", "ESS max apparent power", id="acronym_first"),
        pytest.param("_PropertyEnabled", "Enabled", id="property"),
        pytest.param("_PropertyEnablePT1Filter", "Enable PT1 filter", id="upper_case_with_digits"),
        pytest.param(
            "Tower0Module3Cell002Voltage",
            "Tower 0 module 3 cell 2 voltage",
            id="zero_based_numbers",
        ),
        pytest.param("Tower0Soh", "Tower 0 SoH", id="mixed_case_acronym"),
        pytest.param("V2LvrtEnterThreshold", "V2 LVRT enter threshold", id="version"),
        pytest.param("V2RpmBPointCosPhi", "V2 rpm B point cos φ", id="single_letter"),
        pytest.param("S113Hz", "S113 Hz", id="unit"),
        pytest.param("State28", "State 28", id="number"),
        pytest.param("VBattery1", "V battery 1", id="letter_before_word"),
        pytest.param("", "", id="empty"),
    ],
)
def test_display_name(name: str, expected: str) -> None:
    """Channel names become readable names with the acronyms of the config."""
    assert display_name(name, CONFIG.acronyms) == expected


def test_channels_have_display_names() -> None:
    """Every channel provides its display name."""
    edge = _make_edge(
        {"ess0": {"properties": {}, "channels": [_channel("DcChargeEnergy", "LONG", "Wh_Σ")]}}
    )

    assert edge.components["ess0"].channels[0].display_name == "DC charge energy"


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("INFO", Severity.INFO),
        ("WARNING", Severity.WARNING),
        ("FAULT", Severity.FAULT),
        ("OK", None),
        (None, None),
    ],
)
def test_severity(level: str | None, expected: Severity | None) -> None:
    """Test the mapping of channel levels."""
    assert severity(level) is expected


def _make_edge(components: dict) -> OpenEMSEdge:
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/openems-backend-ui")
    return OpenEMSEdge(backend, "0", {"_host": {"Hostname": "fems1"}, **components})


def _channel(id_: str, type_: str, unit: str = "", **extra: object) -> dict:
    return {"id": id_, "type": type_, "unit": unit, "accessMode": "RO", **extra}


EVCS_CONFIG = {
    "ctrlEvcs0": {
        "_PropertyAlias": "Charging station",
        "factoryId": "Controller.Evcs",
        "properties": {},
        "channels": [
            _channel("_PropertyEnabledCharging", "BOOLEAN"),
            _channel("_PropertyChargeMode", "STRING"),
            _channel("_PropertyEnergySessionLimit", "INTEGER", "Wh"),
            _channel("_PropertyUnclassified", "STRING"),
            _channel("Soc", "INTEGER", "%"),
            _channel("ActiveEnergy", "LONG", "Wh_Σ"),
            _channel(
                "State",
                "INTEGER",
                category="ENUM",
                options={"Ok": 0, "Info": 1, "Warning": 2, "Fault": 3},
            ),
            _channel("ConfigNotActivated", "BOOLEAN", category="STATE", level="FAULT"),
            _channel("Running", "BOOLEAN"),
        ],
    },
    "ctrlGridOptimizedCharge0": {
        "properties": {},
        "channels": [_channel("_PropertyManualTargetTime", "STRING")],
    },
    "unknownComponent0": {"properties": {}, "channels": [_channel("Foo", "INTEGER")]},
}


async def test_channel_kinds() -> None:
    """Test every channel gets its kind and is returned by channels_of."""
    edge = _make_edge(EVCS_CONFIG)
    evcs = edge.components["ctrlEvcs0"]
    kinds = {channel.name: channel.kind for channel in evcs.channels}
    assert kinds == {
        "_PropertyEnabledCharging": ChannelKind.SWITCH,
        "_PropertyChargeMode": ChannelKind.SELECT,
        "_PropertyEnergySessionLimit": ChannelKind.NUMBER,
        "Soc": ChannelKind.SENSOR,
        "ActiveEnergy": ChannelKind.SENSOR,
        "State": ChannelKind.ENUM_SENSOR,
        "ConfigNotActivated": ChannelKind.BINARY_SENSOR,
        "Running": ChannelKind.BINARY_SENSOR,
    }
    assert [c.name for c in evcs.channels_of(ChannelKind.BINARY_SENSOR)] == [
        "ConfigNotActivated",
        "Running",
    ]
    assert isinstance(evcs.channels_of(ChannelKind.SWITCH)[0], OpenEMSBooleanProperty)
    assert isinstance(evcs.channels_of(ChannelKind.SELECT)[0], OpenEMSEnumProperty)
    assert isinstance(evcs.channels_of(ChannelKind.NUMBER)[0], OpenEMSNumberProperty)
    # narrowing to a channel class filters out other classes of the same kind
    assert evcs.channels_of(ChannelKind.SELECT, OpenEMSNumberProperty) == []
    assert {p.name for p in evcs.properties} == {
        "_PropertyEnabledCharging",
        "_PropertyChargeMode",
        "_PropertyEnergySessionLimit",
    }
    # the config of the caller is not modified
    assert "options" in EVCS_CONFIG["ctrlEvcs0"]["channels"][6]
    time_channels = edge.components["ctrlGridOptimizedCharge0"].channels_of(
        ChannelKind.TIME
    )
    assert isinstance(time_channels[0], OpenEMSTimeProperty)


async def test_channel_metadata() -> None:
    """Test key, units, primary and severity of the channels."""
    edge = _make_edge(EVCS_CONFIG)
    evcs = edge.components["ctrlEvcs0"]
    channels = {channel.name: channel for channel in evcs.channels}

    assert channels["_PropertyChargeMode"].key == "charge_mode"
    assert channels["_PropertyChargeMode"].primary
    assert not channels["Soc"].primary

    soc = channels["Soc"]
    assert (soc.unit, soc.quantity, soc.cumulative) == (
        "%",
        Quantity.STATE_OF_CHARGE,
        False,
    )
    energy = channels["ActiveEnergy"]
    assert (energy.unit, energy.quantity, energy.cumulative) == (
        "Wh",
        Quantity.ENERGY,
        True,
    )
    assert channels["State"].unit is None

    assert channels["ConfigNotActivated"].severity is Severity.FAULT
    assert channels["Running"].severity is None

    assert evcs.unique_id == "fems1 ctrlEvcs0"
    assert evcs.factory_id == "Controller.Evcs"
    assert evcs.has_primary_channels
    assert not edge.components["unknownComponent0"].has_primary_channels
    assert channels["Soc"].unique_id == "fems1/0/ctrlEvcs0/Soc"


@pytest.mark.parametrize(
    ("type_", "unit", "measurement"),
    [
        pytest.param("INTEGER", "W", True, id="quantity"),
        pytest.param("INTEGER", "‰Vn", True, id="unit_without_quantity"),
        pytest.param("INTEGER", "", False, id="no_unit"),
        pytest.param("LONG", "Wh_Σ", False, id="cumulative"),
    ],
)
async def test_measurement(type_: str, unit: str, measurement: bool) -> None:
    """Values with a unit are measurements, unless they are counters."""
    edge = _make_edge(
        {"meter0": {"properties": {}, "channels": [_channel("Value", type_, unit)]}}
    )

    assert edge.components["meter0"].channels[0].measurement is measurement


async def test_option_keys_and_values() -> None:
    """Test enum channels provide snake_case option keys as values."""
    edge = _make_edge(EVCS_CONFIG)
    evcs = edge.components["ctrlEvcs0"]
    state = evcs.channels_of(ChannelKind.ENUM_SENSOR, OpenEMSChannel)[0]
    assert state.option_keys == ["ok", "info", "warning", "fault"]
    state.handle_data_update("ctrlEvcs0/State", 2)
    assert state.value == "warning"
    state.handle_data_update("ctrlEvcs0/State", 7)
    assert state.value is None

    charge_mode = evcs.channels_of(ChannelKind.SELECT, OpenEMSEnumProperty)[0]
    assert charge_mode.option_keys == ["force_charge", "excess_power"]
    charge_mode.handle_data_update("ctrlEvcs0/_PropertyChargeMode", "EXCESS_POWER")
    assert charge_mode.value == "excess_power"
    assert charge_mode.to_backend_value("force_charge") == "FORCE_CHARGE"


async def test_option_keys_are_unique() -> None:
    """Test options which only differ in case or separators share one key."""
    edge = _make_edge(
        {
            "meter0": {
                "properties": {},
                "channels": [
                    _channel(
                        "State",
                        "INTEGER",
                        category="ENUM",
                        options={"Off": 0, "OFF": 1, "On": 2},
                    )
                ],
            }
        }
    )
    state = edge.components["meter0"].channels_of(ChannelKind.ENUM_SENSOR)[0]
    assert state.option_keys == ["off", "on"]


async def test_update_value_converts_to_backend_value() -> None:
    """Test writable channels convert option keys and times before sending."""
    edge = _make_edge(EVCS_CONFIG)
    evcs = edge.components["ctrlEvcs0"]
    evcs.update_config = AsyncMock()
    charge_mode = evcs.channels_of(ChannelKind.SELECT)[0]
    await charge_mode.update_value("excess_power")
    # enabledCharging is part of the update group of chargeMode
    evcs.update_config.assert_awaited_once_with(
        [("chargeMode", "EXCESS_POWER"), ("enabledCharging", None)]
    )

    optimized = edge.components["ctrlGridOptimizedCharge0"]
    optimized.update_config = AsyncMock()
    await optimized.channels_of(ChannelKind.TIME)[0].update_value(time(7, 30))
    optimized.update_config.assert_awaited_once_with([("manualTargetTime", "07:30")])

    with pytest.raises(ValueError):
        await charge_mode.update_value("unknown")


async def test_binary_sensor_values() -> None:
    """Test binary sensors provide booleans."""
    edge = _make_edge(EVCS_CONFIG)
    running = edge.components["ctrlEvcs0"].channels_of(
        ChannelKind.BINARY_SENSOR, OpenEMSChannel
    )[1]
    assert running.is_on is None
    running.handle_data_update("ctrlEvcs0/Running", 1)
    assert running.is_on is True
    running.handle_data_update("ctrlEvcs0/Running", 0)
    assert running.is_on is False
    running.handle_data_update("ctrlEvcs0/Running", None)
    assert running.is_on is None


async def test_derived_channels_are_created() -> None:
    """Test derived channels are part of the component channels."""
    edge = _make_edge(
        {
            "battery0": {
                "properties": {},
                "channels": [
                    _channel("MaxCellVoltage", "INTEGER", "mV"),
                    _channel("MinCellVoltage", "INTEGER", "mV"),
                    _channel("Tower0Module0Cell0Voltage", "INTEGER", "mV"),
                    _channel("Tower0Module0Cell1Voltage", "INTEGER", "mV"),
                ],
            }
        }
    )
    sensors = edge.components["battery0"].channels_of(ChannelKind.SENSOR)
    derived = next(c for c in sensors if c.name == "VoltageDifference")
    assert (derived.key, derived.unit, derived.quantity) == (
        "voltage_difference",
        "mV",
        Quantity.VOLTAGE,
    )
    derived.handle_data_update("battery0/MaxCellVoltage", 3400)
    derived.handle_data_update("battery0/MinCellVoltage", 3350)
    assert derived.value == 50

    cells = next(c for c in sensors if c.name == "Tower0Module0CellVoltageDifference")
    cells.handle_data_update("battery0/Tower0Module0Cell0Voltage", 3300)
    cells.handle_data_update("battery0/Tower0Module0Cell1Voltage", 3320)
    assert cells.value == 20


async def test_decidegree_values_are_scaled() -> None:
    """Values in decidegree Celsius are provided in degree Celsius."""
    edge = _make_edge(
        {
            "battery0": {
                "properties": {},
                "channels": [
                    _channel("Tower0Module0TemperatureSensor1", "INTEGER", "°dC")
                ],
            }
        }
    )
    sensor = edge.components["battery0"].channels_of(ChannelKind.SENSOR)[0]
    assert (sensor.unit, sensor.quantity) == ("°C", Quantity.TEMPERATURE)

    sensor.handle_data_update("battery0/Tower0Module0TemperatureSensor1", 231)
    assert sensor.value == 23.1

    sensor.handle_data_update("battery0/Tower0Module0TemperatureSensor1", None)
    assert sensor.value is None


async def test_text_and_float_values() -> None:
    """Text channels are text sensors, floats are numeric values."""
    edge = _make_edge(
        {
            "_host": {
                "Hostname": "fems1",
                "properties": {},
                "channels": [
                    _channel("OsVersion", "STRING"),
                    _channel("CpuTemperature", "FLOAT", "°C"),
                ],
            }
        }
    )
    host = edge.components["_host"]
    os_version = host.channels_of(ChannelKind.TEXT_SENSOR)[0]
    cpu_temperature = host.channels_of(ChannelKind.SENSOR)[0]
    assert os_version.name == "OsVersion"
    assert os_version.unit is None

    os_version.handle_data_update("_host/OsVersion", "Debian 13")
    cpu_temperature.handle_data_update("_host/CpuTemperature", 48.5)
    assert os_version.value == "Debian 13"
    assert cpu_temperature.value == 48.5

    # values of the wrong type are no values
    os_version.handle_data_update("_host/OsVersion", 13)
    cpu_temperature.handle_data_update("_host/CpuTemperature", "hot")
    assert os_version.value is None
    assert cpu_temperature.value is None


def test_heat_pump_sg_ready_defaults() -> None:
    """SG-Ready heat pump component and entities are primary, so enabled by default."""
    for name in (
        "_PropertyMode",
        "_PropertyManualState",
        "_PropertyAutomaticForceOnSoc",
        "_PropertyAutomaticLockSoc",
        "_PropertyAutomaticLockCtrlEnabled",
        "_PropertyAutomaticLockGridBuyPower",
        "_PropertyMinimumSwitchingTime",
        "Status",
    ):
        assert CONFIG.is_primary_channel("ctrlIoHeatPump0", name), name
    assert CONFIG.has_primary_channels("ctrlIoHeatPump0")
    assert not CONFIG.has_primary_channels("ctrlDebugLog0")


def test_to_snake_case_matches_translation_key_format() -> None:
    """Option keys match the format of the translation keys of Home Assistant."""
    assert (
        to_snake_case(
            "Not active. The controller has no flexibility left to delay charge, "
            "because the limitation is nearly zero."
        )
        == "not_active_the_controller_has_no_flexibility_left_to_delay_charge_"
        "because_the_limitation_is_nearly_zero"
    )
