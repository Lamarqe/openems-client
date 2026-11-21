"""Tests for the comparison of edge configurations."""

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openems_client.connection import OpenEMSConfigReader
from openems_client.edge_config import EdgeConfigChange, edge_config_change
from openems_client.exceptions import OpenEMSError


def _channel(id_: str, unit: str = "W", **extra: object) -> dict:
    return {"id": id_, "type": "INTEGER", "unit": unit, "accessMode": "RO", **extra}


CONFIG = {
    "_host": {
        "factoryId": "Core.Host",
        "properties": {},
        "channels": [],
        "Hostname": "fems1",
    },
    "meter0": {
        "factoryId": "Meter.Socomec",
        "properties": {"type": "GRID", "_lastChangeAt": "2026-01-01"},
        "channels": [_channel("ActivePower"), _channel("Frequency", "mHz")],
        "_PropertyAlias": "Grid meter",
    },
    "ess0": {
        "factoryId": "Ess.Generic",
        "properties": {},
        "channels": [_channel("Soc", "%")],
    },
}


def _modified(**changes: object) -> dict:
    config = copy.deepcopy(CONFIG)
    for path, value in changes.items():
        component, key = path.split("__")
        if value is None:
            del config[component][key]
        else:
            config[component][key] = value
    return config


def test_unchanged() -> None:
    """Identical configurations have no change."""
    change = edge_config_change(CONFIG, copy.deepcopy(CONFIG))
    assert not change
    assert change == EdgeConfigChange()


def test_property_and_alias_changes_are_not_structural() -> None:
    """Property values and aliases are provided by channels, they are no change."""
    new = _modified(
        meter0__properties={"type": "PRODUCTION", "_lastChangeAt": "2026-02-01"},
        meter0___PropertyAlias="Production meter",
    )
    assert not edge_config_change(CONFIG, new)


def test_added_and_removed_components() -> None:
    """Added and removed components are reported."""
    new = copy.deepcopy(CONFIG)
    del new["ess0"]
    new["evcs0"] = {"factoryId": "Evcs.Keba", "properties": {}, "channels": []}

    change = edge_config_change(CONFIG, new)

    assert change.added == {"evcs0"}
    assert change.removed == {"ess0"}
    assert not change.changed
    assert change


@pytest.mark.parametrize(
    "channels",
    [
        pytest.param([_channel("ActivePower")], id="channel_removed"),
        pytest.param(
            [
                _channel("ActivePower"),
                _channel("Frequency", "mHz"),
                _channel("Voltage", "mV"),
            ],
            id="channel_added",
        ),
        pytest.param(
            [_channel("ActivePower"), _channel("Frequency", "Hz")], id="unit_changed"
        ),
        pytest.param(
            [
                _channel("ActivePower"),
                _channel("Frequency", "mHz", category="ENUM", options={"A": 1}),
            ],
            id="options_added",
        ),
    ],
)
def test_changed_channels(channels: list[dict]) -> None:
    """Changes of the channel set or the channel attributes are reported."""
    change = edge_config_change(CONFIG, _modified(meter0__channels=channels))
    assert change.changed == {"meter0"}


def test_channel_order_is_ignored() -> None:
    """The order of the channels is no change."""
    new = _modified(meter0__channels=list(reversed(CONFIG["meter0"]["channels"])))
    assert not edge_config_change(CONFIG, new)


def test_changed_factory() -> None:
    """A changed implementation of a component is reported."""
    change = edge_config_change(CONFIG, _modified(meter0__factoryId="Meter.Janitza"))
    assert change.changed == {"meter0"}


async def test_check_edge_components() -> None:
    """The edge configuration is read again and compared with the cached one."""
    reader = OpenEMSConfigReader(MagicMock(), "0")
    current = _modified(meter0__channels=[_channel("ActivePower")])

    async def read_channels(components: dict) -> list[str]:
        for name in components:
            components[name]["channels"] = current[name]["channels"]
        return []

    edge_config = {
        name: {k: v for k, v in comp.items() if k != "channels"}
        for name, comp in current.items()
    }
    with (
        patch.object(reader, "read_edge_config", AsyncMock(return_value=edge_config)),
        patch.object(reader, "read_edge_channels", side_effect=read_channels),
        patch.object(reader, "read_component_info_channels", AsyncMock()),
    ):
        components, change = await reader.check_edge_components(CONFIG)

    assert components == current
    assert change.changed == {"meter0"}


async def test_check_edge_components_incomplete_read_raises() -> None:
    """Missing channels of a component would look like a removal, so it is an error."""
    reader = OpenEMSConfigReader(MagicMock(), "0")
    with (
        patch.object(reader, "read_edge_config", AsyncMock(return_value={})),
        patch.object(reader, "read_edge_channels", AsyncMock(return_value=["ess0"])),
        pytest.raises(OpenEMSError, match="ess0"),
    ):
        await reader.check_edge_components(CONFIG)
