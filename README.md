# openems-client

Async Python client for [OpenEMS](https://openems.io) and FENECON FEMS energy management systems.

It talks to an OpenEMS edge, an OpenEMS backend or the FENECON portal over the
JSON-RPC websocket API, reads the edge configuration (components and channels),
and keeps a live object model of the edge that is updated by `currentData`
push messages. Writing component properties uses JSON-RPC or the REST API.

This library mainly serves the Home Assistant OpenEMS integration, but it does
not depend on Home Assistant.

## Installation

```bash
pip install openems-client
```

Requires Python 3.14 or newer.

## Usage

```python
import asyncio

from openems_client.const import CONN_TYPE_DIRECT_EDGE
from openems_client.connection import OpenEMSConfigReader, OpenEMSWebSocketConnection
from openems_client.openems import OpenEMSBackend


async def main() -> None:
    connection = OpenEMSWebSocketConnection(
        {
            "type": CONN_TYPE_DIRECT_EDGE,
            "host": "192.168.1.10",
            "url": None,
            "username": "guest",
            "password": "user",
        }
    )
    await connection.connect_to_server()
    login_response = await connection.login_to_server()

    reader = OpenEMSConfigReader(connection)
    edges = await reader.read_edges()
    edge_id = edges["edges"][0]["id"]
    reader.set_edge_id(edge_id)
    components = await reader.read_edge_components()

    multi_edge = OpenEMSConfigReader.parse_login_response(login_response)
    backend = OpenEMSBackend(connection, edge_id, multi_edge, components)
    # start() starts the background tasks, stop() closes the connection and ends them
    backend.start()
    ...
    await backend.stop()


asyncio.run(main())
```

An existing `aiohttp.ClientSession` can be passed with
`OpenEMSWebSocketConnection(conn_props, session=session)`. It is used for all
websocket and REST requests and is not closed by the library. Without it, the
connection creates its own session and closes it in `stop()`.

### Errors

Requests to the backend raise the exceptions of `openems_client.exceptions`:

- `OpenEMSConnectionError`: the backend cannot be reached or does not respond in time.
- `OpenEMSAuthError`: the backend rejects the login.
- `OpenEMSError`: the base class of both, also raised when the backend rejects a request.

### Derived channels

Values that OpenEMS does not provide directly can be calculated from other
channels of a component. Placeholders in `id` create one channel per match;
placeholders that only occur in a source turn it into a list of values.

```python
from openems_client.openems import DerivedChannelDefinition

CELL_VOLTAGE_DIFFERENCE = DerivedChannelDefinition(
    id="Tower{tId}Module{mId}CellVoltageDifference",
    sources={"cells": "Tower{tId}Module{mId}Cell{cId}Voltage"},
    value_fn=lambda v: max(v["cells"]) - min(v["cells"]),
    unit="mV",
)

component = backend.the_edge.components["battery0"]
for channel in component.create_derived_channels([CELL_VOLTAGE_DIFFERENCE]):
    channel.add_listener(lambda: print(channel.name, channel.value))
```

### Connection types

| Constant | Target | Default URL |
| --- | --- | --- |
| `CONN_TYPE_DIRECT_EDGE` | OpenEMS edge | `ws://<host>:8085/` |
| `CONN_TYPE_LOCAL_FEMS` | Local FEMS UI websocket | `ws://<host>:80/websocket` |
| `CONN_TYPE_LOCAL_OPENEMS` | Local OpenEMS backend | `ws://<host>:8082/` |
| `CONN_TYPE_WEB_FENECON` | FENECON portal | `wss://portal.fenecon.de/openems-backend-ui2` |
| `CONN_TYPE_CUSTOM_URL` | Any websocket URL | as given in `url` |

## Development

```bash
uv sync
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## License

Apache License 2.0, see [LICENSE](LICENSE).
