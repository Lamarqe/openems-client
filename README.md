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

from openems_client.client import OpenEMSClient
from openems_client.const import CONN_TYPE_DIRECT_EDGE


async def main() -> None:
    client = OpenEMSClient(
        {
            "type": CONN_TYPE_DIRECT_EDGE,
            "host": "192.168.1.10",
            "url": None,
            "username": "guest",
            "password": "user",
        }
    )
    login = await client.connect()  # closes the connection on failure
    edge_id = (await client.read_edges())[0].id
    hostname = await client.read_edge_hostname(edge_id)  # identifies the edge
    components = await client.read_edge_config(edge_id)  # cache this

    # the backend takes over the connection
    backend = client.create_backend(edge_id, components)
    # start() starts the background tasks, stop() closes the connection and ends them
    backend.start()
    ...
    await backend.stop()


asyncio.run(main())
```

For short requests, e.g. to validate credentials, use the client as a context
manager, which closes the connection:

```python
async with OpenEMSClient(conn_props, session) as client:
    login = await client.connect()
    edges = await client.read_edges()
```

`login.multi_edge` is `True` for backends that manage several edges, such as the
FENECON portal. `read_edges()` returns `EdgeInfo` objects with `id`, `online`,
`comment` and `version`.

An existing `aiohttp.ClientSession` can be passed with
`OpenEMSClient(conn_props, session)`. It is used for all websocket and REST
requests and is not closed by the library. Without it, the connection creates
its own session and closes it in `stop()`.

The lower level classes `OpenEMSWebSocketConnection`, `OpenEMSConfigReader` and
`OpenEMSBackend` remain available.

### Checking a cached configuration

Reading the components and their channels takes one request per component, so
consumers usually cache the result of `read_edge_components()`. To find out
whether the cached configuration is still current, read it again and compare:

```python
components, change = await client.check_edge_config(edge_id, cached)
if change:  # components added, removed or changed (factoryId or channels)
    print(change.added, change.removed, change.changed)
```

Property values and aliases are not part of the change, as they are provided by
the `_Property` channels. If the channels of a component cannot be read, an
`OpenEMSError` is raised instead of reporting the component as removed.

### Errors

Requests to the backend raise the exceptions of `openems_client.exceptions`:

- `OpenEMSConnectionError`: the backend cannot be reached or does not respond in time.
- `OpenEMSAuthError`: the backend rejects the login.
- `OpenEMSTimeoutError`: an operation of the edge, e.g. a system update, did not finish in time.
- `OpenEMSError`: the base class of all of them, also raised when the backend rejects a request.

### Writing write-only channels

Channels with `"accessMode": "WO"` (`ChannelKind.WRITE_ONLY`), e.g. setpoints
and commands, are written via the REST API of the edge. This requires a REST app
with write access (`Controller.Api.Rest.ReadWrite`), see `edge.rest_writable`.

```python
channel = component.channels_of(ChannelKind.WRITE_ONLY)[0]
await channel.write_via_rest(5000)  # one write
await channel.write_via_rest(5000, update_cycle=30, timeout=600)  # repeated
```

The value is converted to the type of the channel (`channel.to_rest_value`);
numbers are given in the normalised unit, e.g. °C for channels in °dC. Invalid
values and channels which are not write-only raise `ValueError`, REST errors the
exceptions above. With `update_cycle > 0` the call returns after the first write
and repeats it in the background, until `timeout` seconds passed (0: no
timeout), the channel is written again or the edge stops. Errors of the repeated
writes are logged.

### Availability

`edge.available` and `channel.available` are `True` while the edge is
connected and sends channel data. On a connection loss the values are cleared,
the channels become unavailable and the library reconnects in the background.
They are available again with the first channel data after the reconnect.
If the backend rejects the login of a reconnect, e.g. after a password change,
the reconnects stop and the callback passed with
`backend.start(auth_failed_callback=cb)` is called. The channels stay
unavailable until a new backend is started with valid credentials.
Listeners of the channels are called on every change of the availability.

### Edge version

`edge.version` is the software version of the edge (channel `_meta/Version`),
`None` until it is known. The channel is only subscribed while a listener is
registered with `edge.add_version_listener(cb)`, which returns a function to
remove it. The last known version stays valid while the edge is disconnected,
and listeners are called when it changes, e.g. after a system update.

### Channels

Each component sorts its channels by `kind` (`openems_client.model.ChannelKind`):
read-only values are `SENSOR` (numbers), `ENUM_SENSOR`, `BINARY_SENSOR` or `TEXT_SENSOR` (text, e.g. serial numbers and versions), writable
properties are `NUMBER`, `SELECT`, `SWITCH` or `TIME`, write-only channels
`WRITE_ONLY`. Channels without a known
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
| `display_name` | readable English name without the `_Property` prefix, eg `Max DC charge power`; numbers keep their value (`Tower 0 cell 2 voltage`), acronyms are spelled as in `config/acronyms.json` |
| `unit` | normalised unit, eg `Wh` for `Wh_Σ`, `°C` for `C` and `°dC` (values divided by 10); `None` without unit |
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

### System update

```python
from openems_client.system_update import UpdateStatus

state = await edge.get_system_update_state()
if state.status is UpdateStatus.AVAILABLE:
    print(state.installed_version, "->", state.latest_version)
    state = await edge.install_system_update(
        lambda s: print(s.status, s.percent_completed)
    )
```

`get_system_update_state()` returns a `SystemUpdateState` with `status`
(`UP_TO_DATE`, `AVAILABLE`, `RUNNING`, `UNKNOWN`), `installed_version`,
`latest_version` and, while running, `percent_completed`.

`install_system_update()` triggers the update and polls its state every
`poll_interval` seconds (default 10), passing each state to the optional
callback. While the edge restarts, the state is `UNKNOWN`; the connection loss
is expected. It returns once the edge reports to be up to date, raises the
error of the update request if the edge rejects it, and raises
`OpenEMSTimeoutError` if the update did not finish within `timeout` seconds
(default 900).

### FEMS and release notes

`edge.is_fems` is `True` for FEMS, the OpenEMS distribution of FENECON: for the
FEMS connection types, edges reporting to the FENECON backend and edges named
`fems<number>`. For these, `edge.manufacturer` is `"FENECON"`.

`edge.release_url` is the page with the changes of the software releases: the
changelog page of the FEMS UI or the FENECON portal, the OpenEMS releases on
GitHub otherwise.

FENECON publishes release notes for FEMS (`edge.has_release_notes`):

```python
notes = await edge.get_release_notes("en")  # Markdown or None
```

They cover the releases after the installed up to the latest version of the
last update state (`edge.get_system_update_state()`). Only changes visible to
system owners are included, each release in the requested language, otherwise
English or German. The changelog is read from `https://fenecon.de/fems-changelog`
with the injected session, once per latest version.

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
