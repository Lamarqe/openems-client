"""OpenEMS config entry data access and preparation."""

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import functools
import logging
import time
from typing import Any, TypedDict

import aiohttp
import jsonrpc_base
import jsonrpc_websocket
from yarl import URL

from .const import (
    AUTHENTICATION_FAILED_ERROR_CODE,
    CONN_TYPE_CUSTOM_URL,
    CONNECT_TIMEOUT_SECONDS,
    CURRENT_DATA_TIMEOUT_SECONDS,
    EDGE_CONFIG_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    WEBSOCKET_HEARTBEAT_SECONDS,
)
from .edge_config import EdgeConfigChange, edge_config_change
from .exceptions import OpenEMSAuthError, OpenEMSConnectionError, OpenEMSError
from .helpers import cancel_task, connection_url, wrap_jsonrpc

_LOGGER = logging.getLogger(__name__)


async def backend_call[T](
    awaitable: Awaitable[T], timeout: float | None = REQUEST_TIMEOUT_SECONDS
) -> T:
    """Await a backend call and convert its errors into OpenEMS client errors."""
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError as err:
        raise OpenEMSConnectionError("Timeout waiting for the backend") from err
    except jsonrpc_base.jsonrpc.TransportError as err:
        raise OpenEMSConnectionError(str(err)) from err
    except jsonrpc_base.jsonrpc.ProtocolError as err:
        if err.args and err.args[0] == AUTHENTICATION_FAILED_ERROR_CODE:
            raise OpenEMSAuthError(str(err)) from err
        raise OpenEMSError(str(err)) from err


def _ws_loop_ended(host: str | None, task: asyncio.Task[Any]) -> None:
    """Retrieve the exception of an ended websocket loop to avoid unhandled task errors."""
    if not task.cancelled() and (ex := task.exception()):
        _LOGGER.debug("Websocket to host %s closed: %s", host, ex)


class ConnectionProperties(TypedDict):
    "Type containing the websocket connection parameters."

    host: str | None
    password: str
    type: str
    url: str | None
    username: str


class OpenEMSWebSocketConnection:
    """Class to manage a websocket connection to an OpenEMS system."""

    def __init__(
        self,
        conn_props: ConnectionProperties,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize OpenEMS websocket connection management.

        Without a session, the connection creates its own and closes it in stop().
        """
        if conn_props["type"] == CONN_TYPE_CUSTOM_URL:
            url_prop: str = conn_props["url"]  # type: ignore[assignment]
            self.conn_url = URL(url_prop)
        else:
            self.conn_url = connection_url(
                conn_props["type"],
                conn_props["host"],
            )
        self.conn_type: str = conn_props["type"]
        self.username: str = conn_props["username"]
        self.password: str = conn_props["password"]
        self.session: aiohttp.ClientSession | None = session

        self.rpc_server = jsonrpc_websocket.Server(
            self.conn_url, session=session, heartbeat=WEBSOCKET_HEARTBEAT_SECONDS
        )
        self.rpc_server_task: asyncio.Task[Any] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._last_data_received: float = time.time()

    async def connect_to_server(self) -> None:
        "Establish websocket connection."
        rpc_server_task: asyncio.Task[Any] = await backend_call(
            self.rpc_server.ws_connect(), CONNECT_TIMEOUT_SECONDS
        )
        rpc_server_task.add_done_callback(
            functools.partial(_ws_loop_ended, self.conn_url.host)
        )
        self.rpc_server_task = rpc_server_task

    async def login_to_server(self) -> dict[str, Any]:
        "Authenticate with username and password."
        if not self.rpc_server.connected:
            raise OpenEMSConnectionError("Not connected to the backend")
        return await backend_call(
            self.rpc_server.authenticateWithPassword(
                username=self.username, password=self.password
            )
        )

    def enable_reconnect(self, connection_lost_callback: Callable[[], None]) -> None:
        """Start a tasks which checks for connection losses tries to reconnect afterwards."""
        self._reconnect_task = asyncio.create_task(
            self._reconnect_forever(connection_lost_callback)
        )

    def notify_data_received(self) -> None:
        """Store the timestamp of the last received data. Used for connection loss detection."""
        self._last_data_received = time.time()

    @property
    def reconnect_task(self) -> asyncio.Task[None] | None:
        """The active reconnect task, if any."""
        return self._reconnect_task

    async def _reconnect_forever(
        self, connection_lost_callback: Callable[[], None]
    ) -> None:
        while True:
            # check for an existing connection
            if self.rpc_server_task:
                while True:
                    with contextlib.suppress(
                        jsonrpc_base.jsonrpc.TransportError,
                        jsonrpc_base.jsonrpc.ProtocolError,
                    ):
                        await asyncio.wait([self.rpc_server_task], timeout=10)
                        # check if data was received recently. If not, trigger reconnect.
                        if (
                            time.time() - self._last_data_received
                            > CURRENT_DATA_TIMEOUT_SECONDS
                        ):
                            # treat the connection as lost.
                            _LOGGER.info(
                                "No data updates received from host %s for %d seconds. Triggering reconnect",
                                self.conn_url.host,
                                CURRENT_DATA_TIMEOUT_SECONDS,
                            )
                            await self.rpc_server.close()

                        if self.rpc_server_task.done():
                            # connection lost (either by missing tcp keepalive or by missing data updates).
                            # Trigger reconnect.
                            break

            # connection lost
            _LOGGER.info("Connection to host %s lost", self.conn_url.host)
            self.rpc_server_task = None
            connection_lost_callback()
            await asyncio.sleep(10)

            try:
                await self.connect_to_server()
                _LOGGER.debug("Reconnected to host %s", self.conn_url.host)
                # connected. Lets login
                await self.login_to_server()
                _LOGGER.info("Connection to host %s reestablished", self.conn_url.host)
            except OpenEMSError:
                await asyncio.sleep(10)
            self._last_data_received = time.time()

    async def stop(self) -> None:
        """Close the connection and stop the reconnect logic."""
        await cancel_task(self._reconnect_task)
        self._reconnect_task = None
        await self.rpc_server.close()


class EdgeNotDefinedError(Exception):
    """Raised when no edge is defined in config entry data."""


class OpenEMSConfigReader:
    """Data for managing OpenEMS config entry data."""

    def __init__(
        self, connection: OpenEMSWebSocketConnection, edge_id: str | None = None
    ) -> None:
        """Initialize OpenEMS entry data."""
        self.connection: OpenEMSWebSocketConnection = connection
        self.edge_id: str | None = edge_id

    def set_edge_id(self, edge_id: str) -> None:
        """Set the edge ID to be used for further data requests."""
        self.edge_id = edge_id

    async def read_edge_config(self) -> dict[str, Any]:
        """Read the component list of the edge, without channels."""
        if not self.edge_id:
            raise EdgeNotDefinedError("No edge ID defined for reading components.")

        edge_call = wrap_jsonrpc("getEdgeConfig")
        r = await backend_call(
            self.connection.rpc_server.edgeRpc(edgeId=self.edge_id, payload=edge_call),
            EDGE_CONFIG_TIMEOUT_SECONDS,
        )
        components: dict[str, Any] = r["payload"]["result"]["components"]
        return components

    async def read_edge_components(self) -> dict[str, Any]:
        """Read components of the edge."""
        components = await self.read_edge_config()

        # read properties of all channels of each component
        await self.read_edge_channels(components)

        # read info details from selected channels
        await self.read_component_info_channels(components)

        return components

    async def check_edge_components(
        self, cached: dict[str, Any]
    ) -> tuple[dict[str, Any], EdgeConfigChange]:
        """Read the components of the edge again and compare them with cached ones.

        Return the components and their structural change. Raise OpenEMSError if the
        channels of a component cannot be read, as the change would be incomplete.
        """
        components = await self.read_edge_config()
        if skipped := await self.read_edge_channels(components):
            raise OpenEMSError(
                f"Cannot read the channels of the components {sorted(skipped)}"
            )
        await self.read_component_info_channels(components)
        return components, edge_config_change(cached, components)

    async def read_edge_channels(self, components: dict[str, Any]) -> list[str]:
        """Load channels of each component.

        Components whose channels cannot be read are removed, return their names.
        """
        skipped: list[str] = []
        for component_id in list(components):
            edge_component_call = wrap_jsonrpc(
                "getChannelsOfComponent",
                componentId=component_id,
            )
            try:
                edge_call = wrap_jsonrpc(
                    "componentJsonApi",
                    componentId="_componentManager",
                    payload=edge_component_call,
                )
                r = await backend_call(
                    self.connection.rpc_server.edgeRpc(
                        edgeId=self.edge_id,
                        payload=edge_call,
                    )
                )
                components[component_id]["channels"] = r["payload"]["result"][
                    "channels"
                ]
            except OpenEMSError:
                _LOGGER.warning(
                    "_read_edge_channels: could not read channels of component %s, skipping",
                    component_id,
                )
                del components[component_id]
                skipped.append(component_id)
        return skipped

    async def read_component_info_channels(self, components: dict[str, Any]) -> None:
        """Read hostname and all component names of an edge."""

        # Look up all components which have a channel "_PropertyAlias"
        alias_components = []
        for comp_name, comp in components.items():
            for chan in comp["channels"]:
                if chan["id"] == "_PropertyAlias":
                    alias_components.append(comp_name)
                    continue

        config_channels = ["_host/Hostname"]
        config_channels.extend(c + "/_PropertyAlias" for c in alias_components)
        data = await self.get_channel_values_via_websocket(config_channels)

        # store component aliases and hostname in the json config of the component
        for address, value in data.items():
            component, channel = address.split("/")
            if component in components:
                components[component][channel] = value

    async def get_channel_values_via_websocket(
        self, channels: list[str]
    ) -> dict[str, Any]:
        """Read channels via dedicated websocket connection."""
        if not self.edge_id:
            raise EdgeNotDefinedError("No edge ID defined for reading components.")

        # create new connection and login
        rpc_server = jsonrpc_websocket.Server(
            url=self.connection.conn_url,
            session=self.connection.session,
            heartbeat=WEBSOCKET_HEARTBEAT_SECONDS,
        )

        ws_loop_task = await backend_call(
            rpc_server.ws_connect(), CONNECT_TIMEOUT_SECONDS
        )
        ws_loop_task.add_done_callback(
            functools.partial(_ws_loop_ended, self.connection.conn_url.host)
        )
        _LOGGER.debug("wsocket component info request: login")
        await backend_call(
            rpc_server.authenticateWithPassword(
                username=self.connection.username, password=self.connection.password
            )
        )

        # prepare callback event and subscribe for the required data
        data_received: asyncio.Event = asyncio.Event()
        data: dict[str, Any] = {}

        def _handle_callback(**kwargs: Any) -> None:
            if kwargs["payload"]["method"] != "currentData":
                # ignore
                return

            nonlocal data
            data = kwargs["payload"]["params"]
            data_received.set()

        rpc_server.edgeRpc = _handle_callback
        await backend_call(rpc_server.subscribeEdges(edges=[self.edge_id]))

        subscribe_call = wrap_jsonrpc("subscribeChannels", count=0, channels=channels)
        # pyright: ignore[reportGeneralTypeIssues]
        await backend_call(
            rpc_server.edgeRpc(edgeId=self.edge_id, payload=subscribe_call)
        )

        # wait for the data. When received, close connection and return data
        await backend_call(data_received.wait(), timeout=5)
        await rpc_server.close()

        return data

    async def read_edges(self) -> dict[str, Any]:
        """Request list of all edges."""
        edges: dict[str, Any] = await backend_call(
            self.connection.rpc_server.getEdges(page=0, limit=20, searchParams={})
        )
        return edges

    @staticmethod
    def parse_login_response(login_response: dict[str, Any]) -> bool:
        """Parse login response to determine if backend has multiple edges."""
        if user_dict := login_response.get("user"):
            return bool(user_dict["hasMultipleEdges"])
        # Fallback to true. Should never happen.
        return True
