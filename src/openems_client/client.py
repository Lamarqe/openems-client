"""Entry point for connecting to an OpenEMS system."""

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import aiohttp

from .connection import (
    ConnectionProperties,
    OpenEMSConfigReader,
    OpenEMSWebSocketConnection,
)
from .edge_config import EdgeConfigChange
from .openems import OpenEMSBackend


@dataclass(frozen=True, kw_only=True)
class LoginInfo:
    """Result of a successful login."""

    # the backend manages several edges, eg the FENECON portal
    multi_edge: bool


@dataclass(frozen=True, kw_only=True)
class EdgeInfo:
    """Edge available to the logged in user."""

    id: str
    online: bool
    comment: str | None = None
    version: str | None = None


class OpenEMSClient:
    """Connection to an OpenEMS system, from login to a running backend.

    Usage for short requests, eg a config flow:

        async with OpenEMSClient(conn_props, session) as client:
            login = await client.connect()
            edges = await client.read_edges()

    Long running usage: connect, read the edge configuration and create the
    backend with create_backend(). The backend then owns the connection,
    backend.stop() closes it.
    """

    def __init__(
        self,
        conn_props: ConnectionProperties,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Prepare the connection, raise ValueError for an invalid host or URL."""
        self.connection = OpenEMSWebSocketConnection(conn_props, session)
        self._login: LoginInfo | None = None
        self._backend_created = False
        self._closed = False

    @property
    def host(self) -> str | None:
        """Return the host of the connection URL."""
        return self.connection.conn_url.host

    @property
    def login(self) -> LoginInfo:
        """Return the result of connect(), raise RuntimeError before."""
        if self._login is None:
            raise RuntimeError("Not logged in, call connect() first")
        return self._login

    async def connect(self) -> LoginInfo:
        """Connect and log in.

        Raise OpenEMSAuthError if the login is rejected, OpenEMSConnectionError if
        the system cannot be reached and OpenEMSError for other failures. The
        connection is closed on failure.
        """
        self._closed = False
        try:
            await self.connection.connect_to_server()
            response = await self.connection.login_to_server()
        except BaseException:
            await self.close()
            raise
        self._login = LoginInfo(
            multi_edge=OpenEMSConfigReader.parse_login_response(response)
        )
        return self._login

    async def read_edges(self) -> list[EdgeInfo]:
        """Return the edges available to the logged in user."""
        response = await OpenEMSConfigReader(self.connection).read_edges()
        return [
            EdgeInfo(
                id=edge["id"],
                online=bool(edge.get("isOnline")),
                comment=edge.get("comment") or None,
                version=edge.get("version") or None,
            )
            for edge in response["edges"]
        ]

    async def read_edge_hostname(self, edge_id: str) -> str:
        """Return the hostname of the edge, which identifies it.

        On multi edge backends, the edge ID is appended ("<hostname> <edge id>"), as
        in OpenEMSEdge.hostname. Raise OpenEMSError if the edge does not respond.
        """
        reader = OpenEMSConfigReader(self.connection, edge_id)
        # the edge configuration is read to verify that the edge is running
        await reader.read_edge_config()
        data = await reader.get_channel_values_via_websocket(["_host/Hostname"])
        hostname: str = data["_host/Hostname"]
        if self.login.multi_edge:
            hostname += " " + edge_id
        return hostname

    async def read_edge_config(self, edge_id: str) -> dict[str, Any]:
        """Read the components of the edge including their channels.

        Takes one request per component, so callers usually cache the result.
        """
        return await OpenEMSConfigReader(
            self.connection, edge_id
        ).read_edge_components()

    async def check_edge_config(
        self, edge_id: str, cached: dict[str, Any]
    ) -> tuple[dict[str, Any], EdgeConfigChange]:
        """Read the edge configuration again and compare it with a cached one.

        See OpenEMSConfigReader.check_edge_components.
        """
        return await OpenEMSConfigReader(
            self.connection, edge_id
        ).check_edge_components(cached)

    def create_backend(
        self, edge_id: str, components: dict[str, Any]
    ) -> OpenEMSBackend:
        """Create the backend for the edge, which takes over the connection.

        No background tasks are started before backend.start().
        """
        backend = OpenEMSBackend(
            self.connection, edge_id, self.login.multi_edge, components
        )
        self._backend_created = True
        return backend

    async def close(self) -> None:
        """Close the connection, unless it is closed or was handed over to a backend."""
        if not self._backend_created and not self._closed:
            self._closed = True
            await self.connection.stop()

    async def __aenter__(self) -> Self:
        """Return the client, which is closed when leaving the context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the connection."""
        await self.close()
