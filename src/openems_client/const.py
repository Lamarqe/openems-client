"""Constants for the OpenEMS client."""

from typing import TypedDict

SLASH_ESC = "_s_l_a_s_h_"

CONN_TYPE_DIRECT_EDGE: str = "direct_edge"
CONN_TYPE_LOCAL_FEMS: str = "local_fems"
CONN_TYPE_LOCAL_OPENEMS: str = "local_openems"
CONN_TYPE_WEB_FENECON: str = "web_fenecon"
CONN_TYPE_CUSTOM_URL: str = "custom_url"
CONN_TYPE_REST: str = "rest"

CURRENT_DATA_TIMEOUT_SECONDS = 60
CONNECT_TIMEOUT_SECONDS = 10
REQUEST_TIMEOUT_SECONDS = 10
# the edge config of large systems takes longer to transfer
EDGE_CONFIG_TIMEOUT_SECONDS = 30
AUTHENTICATION_FAILED_ERROR_CODE = 1003
WEBSOCKET_HEARTBEAT_SECONDS = 60


class ConnectionType(TypedDict):
    "Type containing the websocket connection parameters."

    scheme: str
    host: str | None
    port: int
    path: str


CONN_TYPES: dict[str, ConnectionType] = {
    CONN_TYPE_DIRECT_EDGE: {
        "scheme": "ws",
        "host": None,
        "port": 8085,
        "path": "/",
    },
    CONN_TYPE_LOCAL_FEMS: {
        "scheme": "ws",
        "host": None,
        "port": 80,
        "path": "/websocket",
    },
    CONN_TYPE_LOCAL_OPENEMS: {
        "scheme": "ws",
        "host": None,
        "port": 8082,
        "path": "/",
    },
    CONN_TYPE_WEB_FENECON: {
        "scheme": "wss",
        "host": "portal.fenecon.de",
        "port": 443,
        "path": "/openems-backend-ui2",
    },
    CONN_TYPE_REST: {
        "scheme": "http",
        "host": None,
        "port": 8084,
        "path": "/rest/",
    },
}
