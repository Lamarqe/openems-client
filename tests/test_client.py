"""Tests for the OpenEMSClient entry point."""

from unittest.mock import AsyncMock, patch

import pytest

from openems_client.client import EdgeInfo, LoginInfo, OpenEMSClient
from openems_client.connection import ConnectionProperties, OpenEMSConfigReader
from openems_client.const import CONN_TYPE_DIRECT_EDGE
from openems_client.edge_config import EdgeConfigChange
from openems_client.exceptions import OpenEMSAuthError, OpenEMSConnectionError

CONN_PROPS = ConnectionProperties(
    host="192.0.2.1",
    password="secret",
    type=CONN_TYPE_DIRECT_EDGE,
    url=None,
    username="user",
)


def _client(multi_edge: bool = False) -> OpenEMSClient:
    """Return a client, whose connection is mocked."""
    client = OpenEMSClient(CONN_PROPS)
    client.connection.connect_to_server = AsyncMock()
    client.connection.login_to_server = AsyncMock(
        return_value={"user": {"hasMultipleEdges": multi_edge}}
    )
    client.connection.stop = AsyncMock()
    return client


@pytest.mark.parametrize("multi_edge", [False, True])
async def test_connect(multi_edge: bool) -> None:
    """Connecting logs in and reports whether the backend manages several edges."""
    client = _client(multi_edge)

    assert await client.connect() == LoginInfo(multi_edge=multi_edge)
    assert client.login.multi_edge is multi_edge
    assert client.host == "192.0.2.1"
    client.connection.connect_to_server.assert_awaited_once()
    client.connection.login_to_server.assert_awaited_once()
    client.connection.stop.assert_not_awaited()


@pytest.mark.parametrize(
    ("step", "error"),
    [
        pytest.param(
            "connect_to_server", OpenEMSConnectionError("refused"), id="connect"
        ),
        pytest.param("login_to_server", OpenEMSAuthError("denied"), id="login"),
    ],
)
async def test_connect_error_closes_connection(step: str, error: Exception) -> None:
    """A failed connect or login is raised and closes the connection."""
    client = _client()
    getattr(client.connection, step).side_effect = error

    with pytest.raises(type(error)):
        await client.connect()

    client.connection.stop.assert_awaited_once()
    with pytest.raises(RuntimeError):
        client.login  # noqa: B018


async def test_read_edges() -> None:
    """The edges of the user are returned as EdgeInfo."""
    client = _client()
    response = {
        "edges": [
            {"id": "0", "isOnline": True, "comment": "", "version": "2026.7.2"},
            {"id": "1", "isOnline": False, "comment": "Garage"},
        ]
    }
    with patch.object(
        OpenEMSConfigReader, "read_edges", AsyncMock(return_value=response)
    ):
        edges = await client.read_edges()

    assert edges == [
        EdgeInfo(id="0", online=True, comment=None, version="2026.7.2"),
        EdgeInfo(id="1", online=False, comment="Garage", version=None),
    ]


@pytest.mark.parametrize(
    ("multi_edge", "hostname"),
    [(False, "fems1"), (True, "fems1 0")],
)
async def test_read_edge_hostname(multi_edge: bool, hostname: str) -> None:
    """The hostname identifies the edge, multi edge backends append the edge ID."""
    client = _client(multi_edge)
    await client.connect()
    read_edge_config = AsyncMock(return_value={})
    with (
        patch.object(OpenEMSConfigReader, "read_edge_config", read_edge_config),
        patch.object(
            OpenEMSConfigReader,
            "get_channel_values_via_websocket",
            AsyncMock(return_value={"_host/Hostname": "fems1"}),
        ),
    ):
        assert await client.read_edge_hostname("0") == hostname

    # verifies that the edge is running
    read_edge_config.assert_awaited_once()


async def test_read_and_check_edge_config() -> None:
    """Reading and checking the configuration use the reader for the given edge."""
    client = _client()
    components = {"meter0": {"channels": []}}
    result = (components, EdgeConfigChange())
    with (
        patch.object(
            OpenEMSConfigReader,
            "read_edge_components",
            AsyncMock(return_value=components),
        ),
        patch.object(
            OpenEMSConfigReader,
            "check_edge_components",
            AsyncMock(return_value=result),
        ) as check,
    ):
        assert await client.read_edge_config("0") == components
        assert await client.check_edge_config("0", components) == result

    check.assert_awaited_once_with(components)


async def test_create_backend_takes_over_connection() -> None:
    """The backend owns the connection, closing the client does not close it."""
    client = _client(multi_edge=True)
    await client.connect()

    backend = client.create_backend("0", {"_host": {"Hostname": "fems1"}})

    assert backend.connection is client.connection
    assert backend.multi_edge
    assert backend.the_edge.hostname == "fems1 0"
    await client.close()
    client.connection.stop.assert_not_awaited()


async def test_create_backend_requires_login() -> None:
    """A backend can only be created after the login."""
    with pytest.raises(RuntimeError):
        _client().create_backend("0", {"_host": {"Hostname": "fems1"}})


async def test_context_manager_closes_connection() -> None:
    """Leaving the context closes the connection."""
    client = _client()
    async with client as entered:
        assert entered is client
        await client.connect()

    client.connection.stop.assert_awaited_once()


def test_invalid_url_raises_value_error() -> None:
    """Invalid connection properties are reported as ValueError."""
    with pytest.raises(ValueError):
        OpenEMSClient(ConnectionProperties(**{**CONN_PROPS, "host": "in valid"}))


async def test_failed_connect_in_context_closes_once() -> None:
    """A failed connect closes the connection, leaving the context does not again."""
    client = _client()
    client.connection.connect_to_server.side_effect = OpenEMSConnectionError("refused")

    with pytest.raises(OpenEMSConnectionError):
        async with client:
            await client.connect()

    client.connection.stop.assert_awaited_once()
