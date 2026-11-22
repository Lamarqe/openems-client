"""Tests for write-only channels and their REST writes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from yarl import URL

from openems_client.model import ChannelKind
from openems_client.openems import OpenEMSChannel, OpenEMSEdge


def _channel(id_: str, type_: str, unit: str = "", access_mode: str = "WO") -> dict:
    return {"id": id_, "type": type_, "unit": unit, "accessMode": access_mode}


def _make_edge(rest_mode: str | None = "ReadWrite") -> OpenEMSEdge:
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/")
    config: dict = {
        "_host": {"Hostname": "fems1"},
        "batteryInverter0": {
            "properties": {},
            "channels": [
                _channel("PeakShavingPowerLimit", "INTEGER", "W"),
                _channel("FastChargeEnable", "BOOLEAN"),
                _channel("DeviceLicence", "STRING"),
                _channel("Setpoint", "FLOAT"),
                _channel("TargetTemperature", "INTEGER", "°dC"),
                _channel("ActivePower", "INTEGER", "W", access_mode="RO"),
            ],
        },
    }
    if rest_mode:
        config["ctrlApiRest0"] = {
            "factoryId": f"Controller.Api.Rest.{rest_mode}",
            "properties": {},
        }
    return OpenEMSEdge(backend, "0", config)


def _write_only(edge: OpenEMSEdge) -> dict[str, OpenEMSChannel]:
    return {
        channel.name: channel
        for channel in edge.components["batteryInverter0"].channels_of(
            ChannelKind.WRITE_ONLY, OpenEMSChannel
        )
    }


def test_write_only_channels_have_their_own_kind() -> None:
    """Write-only channels are no sensors, whatever their type."""
    component = _make_edge().components["batteryInverter0"]

    assert set(_write_only(_make_edge())) == {
        "PeakShavingPowerLimit",
        "FastChargeEnable",
        "DeviceLicence",
        "Setpoint",
        "TargetTemperature",
    }
    assert [c.name for c in component.channels_of(ChannelKind.SENSOR)] == [
        "ActivePower"
    ]
    assert not component.channels_of(ChannelKind.BINARY_SENSOR)
    assert not component.channels_of(ChannelKind.TEXT_SENSOR)


@pytest.mark.parametrize(
    ("rest_mode", "writable"),
    [("ReadWrite", True), ("ReadOnly", False), (None, False)],
)
def test_rest_writable(rest_mode: str | None, writable: bool) -> None:
    """Only a REST app with write access allows REST writes."""
    assert _make_edge(rest_mode).rest_writable is writable


@pytest.mark.parametrize(
    ("channel", "value", "expected"),
    [
        pytest.param("PeakShavingPowerLimit", 5000, 5000, id="int"),
        pytest.param("PeakShavingPowerLimit", 5000.0, 5000, id="integral_float"),
        pytest.param("PeakShavingPowerLimit", "4000", 4000, id="numeric_text"),
        pytest.param("FastChargeEnable", "on", True, id="boolean_text"),
        pytest.param("FastChargeEnable", 0, False, id="boolean_int"),
        pytest.param("DeviceLicence", "ABC-123", "ABC-123", id="text"),
        pytest.param("DeviceLicence", 42, "42", id="number_as_text"),
        pytest.param("Setpoint", 1.5, 1.5, id="float"),
        pytest.param("TargetTemperature", 23.1, 231, id="decidegree"),
    ],
)
def test_to_rest_value(channel: str, value: object, expected: object) -> None:
    """Values are converted to the type and the unit of the channel."""
    result = _write_only(_make_edge())[channel].to_rest_value(value)
    assert result == expected
    assert type(result) is type(expected)


@pytest.mark.parametrize(
    ("channel", "value"),
    [
        pytest.param("PeakShavingPowerLimit", 1.5, id="fraction_for_integer"),
        pytest.param("PeakShavingPowerLimit", "much", id="text_for_integer"),
        pytest.param("PeakShavingPowerLimit", True, id="boolean_for_integer"),
        pytest.param("FastChargeEnable", 2, id="number_for_boolean"),
        pytest.param("DeviceLicence", True, id="boolean_for_text"),
        pytest.param("DeviceLicence", None, id="none_for_text"),
    ],
)
async def test_invalid_values_raise_before_writing(channel: str, value: object) -> None:
    """Invalid values raise ValueError without a request."""
    edge = _make_edge()
    edge.backend.connection.session.post = MagicMock()

    with pytest.raises(ValueError, match="Invalid"):
        await _write_only(edge)[channel].write_via_rest(value)

    edge.backend.connection.session.post.assert_not_called()


async def test_only_write_only_channels_are_written() -> None:
    """Channels with read access are not written via REST."""
    edge = _make_edge()
    active_power = edge.components["batteryInverter0"].channels_of(
        ChannelKind.SENSOR, OpenEMSChannel
    )[0]

    with pytest.raises(ValueError, match="not write-only"):
        await active_power.write_via_rest(1)


async def test_background_tasks_end_with_the_edge() -> None:
    """Background tasks of the edge are cancelled when it stops."""
    edge = _make_edge()
    task = edge.create_background_task(asyncio.Event().wait())
    await asyncio.sleep(0)

    await edge.stop()

    assert task.cancelled()


async def test_write_sends_converted_value() -> None:
    """The converted value is posted to the REST API of the channel."""
    edge = _make_edge()
    post_context = MagicMock()
    post_context.__aenter__ = AsyncMock(return_value=MagicMock(status=200))
    post_context.__aexit__ = AsyncMock(return_value=None)
    edge.backend.connection.session.post = MagicMock(return_value=post_context)

    await _write_only(edge)["TargetTemperature"].write_via_rest("21.5")

    call = edge.backend.connection.session.post.call_args
    assert str(call.args[0]).endswith(
        "/rest/channel/batteryInverter0/TargetTemperature"
    )
    assert call.kwargs["json"] == {"value": 215}
