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

REST writes (`channel.write_via_rest`) raise these exceptions as well. With
`update_cycle > 0` the call returns after the first write and repeats the
write in the background; errors of the repeated writes are logged.

### Availability

`edge.available` and `channel.available` are `True` while the edge is
connected and sends channel data. On a connection loss the values are cleared,
the channels become unavailable and the library reconnects in the background.
They are available again with the first channel data after the reconnect.
Listeners of the channels are called on every change of the availability.

### Channels

Each component sorts its channels by `kind` (`openems_client.model.ChannelKind`):
read-only values are `SENSOR`, `ENUM_SENSOR` or `BINARY_SENSOR`, writable
properties are `NUMBER`, `SELECT`, `SWITCH` or `TIME`. Channels without a known
representation, eg string properties without options, are skipped.

```python
from openems_client.model import ChannelKind
from openems_client.openems import OpenEMSNumberProperty

component = backend.the_edge.components["ctrlEvcs0"]
for channel in component.channels_of(ChannelKind.SENSOR):
    print(channel.key, channel.unit, channel.quantity, channel.cumulative)
for number in component.channels_of(ChannelKind.NUMBER, OpenEMSNumberProperty):
    print(number.key, number.lower_limit, number.upper_limit, number.step)
```

Every channel provides:

| Attribute | Meaning |
| --- | --- |
| `unique_id` | `<hostname>/<edge id>/<component>/<channel>` |
| `key` | snake_case channel name without the `_Property` prefix, eg `charge_mode` |
| `unit` | normalised unit, eg `Wh` for `Wh_Σ`, `°C` for `C`; `None` without unit |
| `quantity` | `openems_client.model.Quantity`, eg `ENERGY`, `STATE_OF_CHARGE` |
| `cumulative` | the value is a counter which only increases (`_Σ` units) |
| `primary` | one of the main channels of the component (`config/primary_channels.json`) |
| `severity` | level of state channels which signal a problem |
| `option_keys` | snake_case keys of the options of `ENUM_SENSOR` and `SELECT` channels |
| `value` | current value; the option key for enums, a `bool` for binary sensors and switches |
| `available` | the edge is connected and sends channel data |
| `add_listener(cb)` | call `cb` on value and availability changes, subscribes the channel at the edge |

Writable channels accept new values with `await channel.update_value(value)`,
`SELECT` channels take an option key.

Components provide `unique_id` (`"<hostname> <component>"`), `alias`,
`factory_id` and `has_primary_channels`.

### Derived channels

Values that OpenEMS does not provide directly are calculated from other
channels of a component, eg the cell voltage differences of batteries. They are
`SENSOR` channels of the component (definitions in `openems.DERIVED_CHANNELS`).
Placeholders in `id` create one channel per match; placeholders that only occur
in a source turn it into a list of values. Further definitions can be applied
with `component.create_derived_channels(...)`:

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
