"""Tests for the availability of the edge and the REST write errors."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from openems_client.exceptions import OpenEMSConnectionError, OpenEMSError
from openems_client.model import ChannelKind
from openems_client.openems import OpenEMSChannel, OpenEMSEdge


def _channel(id_: str, type_: str = "INTEGER", access_mode: str = "RO") -> dict:
    return {"id": id_, "type": type_, "unit": "W", "accessMode": access_mode}


def _make_edge(rest: bool = False) -> OpenEMSEdge:
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/")
    backend.connection.username = "user"
    backend.connection.password = "pass"
    config: dict = {
        "_host": {"Hostname": "fems1"},
        "meter0": {
            "properties": {},
            "channels": [
                _channel("ActivePower"),
                _channel("Frequency"),
                _channel("SetActivePowerEquals", access_mode="WO"),
            ],
        },
    }
    if rest:
        config["ctrlApiRest0"] = {
            "factoryId": "Controller.Api.Rest.ReadWrite",
            "properties": {"port": 8084},
        }
    return OpenEMSEdge(backend, "0", config)


def _sensors(edge: OpenEMSEdge) -> list[OpenEMSChannel]:
    return edge.components["meter0"].channels_of(ChannelKind.SENSOR, OpenEMSChannel)


def _write_only(edge: OpenEMSEdge) -> OpenEMSChannel:
    return edge.components["meter0"].channels_of(
        ChannelKind.WRITE_ONLY, OpenEMSChannel
    )[0]


async def test_available_with_channel_data() -> None:
    """The edge and its channels are available once channel data arrives."""
    edge = _make_edge()
    power, frequency = _sensors(edge)
    power_listener = MagicMock()
    frequency_listener = MagicMock()
    power.add_listener(power_listener)
    frequency.add_listener(frequency_listener)

    assert not edge.available
    assert not power.available

    edge.currentData({"meter0/ActivePower": 1500})

    assert edge.available
    assert power.available
    assert power.value == 1500
    # listeners of channels without a new value learn about the availability
    frequency_listener.assert_called_once()
    assert power_listener.call_count == 2


async def test_unavailable_on_connection_loss() -> None:
    """A connection loss clears the values and makes all channels unavailable."""
    edge = _make_edge()
    power, frequency = _sensors(edge)
    frequency_listener = MagicMock()
    power.add_listener(MagicMock())
    frequency.add_listener(frequency_listener)
    edge.currentData({"meter0/ActivePower": 1500})
    frequency_listener.reset_mock()

    edge.set_unavailable()

    assert not edge.available
    assert not power.available
    assert power.value is None
    frequency_listener.assert_called_once()

    edge.currentData({"meter0/ActivePower": 1400})

    assert power.available
    assert power.value == 1400


def _mock_post(edge: OpenEMSEdge, **response: object) -> MagicMock:
    post_context = MagicMock()
    post_context.__aenter__ = AsyncMock(return_value=MagicMock(**response))
    post_context.__aexit__ = AsyncMock(return_value=None)
    post = MagicMock(return_value=post_context)
    edge.backend.connection.session.post = post
    return post


async def test_rest_write_without_rest_app_raises() -> None:
    """Writing via REST needs a REST app with write access."""
    edge = _make_edge()
    with pytest.raises(OpenEMSError, match="no REST app"):
        await _write_only(edge).write_via_rest(1)


async def test_rest_write_http_error_raises() -> None:
    """A rejected REST write raises OpenEMSError."""
    edge = _make_edge(rest=True)
    _mock_post(edge, status=401, reason="Unauthorized")
    with pytest.raises(OpenEMSError, match="HTTP 401"):
        await _write_only(edge).write_via_rest(1)


async def test_rest_write_connection_error_raises() -> None:
    """An unreachable REST API raises OpenEMSConnectionError."""
    edge = _make_edge(rest=True)
    edge.backend.connection.session.post = MagicMock(
        side_effect=aiohttp.ClientConnectionError("refused")
    )
    with pytest.raises(OpenEMSConnectionError):
        await _write_only(edge).write_via_rest(1)


async def test_rest_write_cyclic_returns_and_repeats() -> None:
    """A cyclic write returns after the first write and repeats in the background.

    A new write of the channel replaces it, stopping the edge ends it.
    """
    edge = _make_edge(rest=True)
    post = _mock_post(edge, status=200)
    channel = _write_only(edge)

    await channel.write_via_rest(1, update_cycle=0.01)
    assert post.call_count == 1
    await asyncio.sleep(0.05)
    assert post.call_count > 2

    await channel.write_via_rest(2)
    calls = post.call_count
    await asyncio.sleep(0.03)
    assert post.call_count == calls
    assert post.call_args.kwargs["json"] == {"value": 2}

    await channel.write_via_rest(3, update_cycle=0.01)
    await edge.stop()
    calls = post.call_count
    await asyncio.sleep(0.03)
    assert post.call_count == calls


async def test_rest_write_cyclic_ends_after_timeout() -> None:
    """Repeated writes end after the timeout, their errors are only logged."""
    edge = _make_edge(rest=True)
    post = _mock_post(edge, status=200)
    channel = _write_only(edge)

    await channel.write_via_rest(1, update_cycle=0.01, timeout=0.035)
    post.return_value.__aenter__.return_value.status = 500
    await asyncio.sleep(0.1)

    assert 2 <= post.call_count <= 5
    assert channel._rest_update_task.done()


async def test_rest_write_basic_auth_uses_latin1() -> None:
    """The credentials are encoded as latin-1, as with the former aiohttp.BasicAuth."""
    edge = _make_edge(rest=True)
    edge.backend.connection.password = "pä"
    post = _mock_post(edge, status=200)

    await _write_only(edge).write_via_rest(1)

    assert post.call_args.kwargs["headers"] == {"Authorization": "Basic dXNlcjpw5A=="}
