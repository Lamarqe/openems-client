"""OpenEMS API."""

from abc import abstractmethod
import asyncio
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import time
import logging
import math
from typing import Any, NamedTuple

import aiohttp
from jinja2 import Template
import jsonrpc_base
from yarl import URL

from .config import OpenEMSConfig
from .connection import OpenEMSWebSocketConnection, backend_call
from .const import CONN_TYPE_REST, CONN_TYPE_WEB_FENECON, CONN_TYPES, SLASH_ESC
from .exceptions import OpenEMSConnectionError
from .helpers import cancel_task, connection_url, to_snake_case, wrap_jsonrpc
from .helpers_openems import (
    OpenEMSAccessMode,
    access_mode,
    expand_derived_definition,
    prepare_ref_value,
)

_LOGGER = logging.getLogger(__name__)


class RestDetails(NamedTuple):
    """Details about the REST connection."""

    mode: str
    url: URL


CONFIG: OpenEMSConfig = OpenEMSConfig()

# value of a channel as received from the backend
type BackendValue = str | int | float | None
# value of a channel as provided by the channel objects
type ChannelValue = str | int | float | bool | time | None


class OpenEMSDataHandler:
    """Interface for handling data updates from the backend."""

    def __init__(self, component: OpenEMSComponent, name: str) -> None:
        """Initialize the handler."""
        self.component: OpenEMSComponent = component
        self.name: str = name
        self._listeners: list[Callable[[], None]] = []
        self._current_value: Any = None

    @abstractmethod
    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""

    @abstractmethod
    def _subscribe(self) -> None:
        """Subscribe the channels needed for the value at the edge."""

    @abstractmethod
    def _unsubscribe(self) -> None:
        """Remove the subscriptions of the channel at the edge."""

    @property
    def value(self) -> ChannelValue:
        """Return the value of the channel."""

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Add a listener called on value changes, return a function to remove it.

        The channel is subscribed at the edge while it has listeners.
        """
        if not self._listeners:
            self._subscribe()
        self._listeners.append(listener)

        def remove_listener() -> None:
            self._listeners.remove(listener)
            if not self._listeners:
                self._unsubscribe()

        return remove_listener

    def _notify_listeners(self) -> None:
        for listener in list(self._listeners):
            listener()

    def unique_id(self) -> str:
        """Generate unique ID for the channel."""
        return (
            self.component.edge.hostname
            + "/"
            + self.component.edge.id
            + "/"
            + self.component.name
            + "/"
            + self.name
        )


class OpenEMSChannel(OpenEMSDataHandler):
    """Class representing a sensor of an OpenEMS component."""

    # Use with platform sensor
    def __init__(
        self,
        component: OpenEMSComponent,
        channel_json: dict[str, Any],
        options: dict[str, int] | None = None,
    ) -> None:
        """Initialize the channel."""
        super().__init__(component, channel_json["id"])
        unit = channel_json["unit"]
        self.options: dict[int, str] | None = None
        if (
            "category" in channel_json
            and channel_json["category"] == "ENUM"
            and options is not None
        ):
            self.options = {v: k for k, v in options.items()}

        self.unit: str = unit
        self.orig_json: dict[str, Any] = channel_json
        self._rest_update_task: asyncio.Task[None] | None = None

    def handle_current_value(self, value: Any) -> None:
        """Handle a new entity value and notify Home Assistant."""
        if value != self._current_value:
            self._current_value = value
            self._notify_listeners()

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        if value is not None and isinstance(value, int):
            if self.options is not None:
                self.handle_current_value(self.options.get(value))
            else:
                self.handle_current_value(value)
        else:
            self.handle_current_value(None)

    @property
    def value(self) -> ChannelValue:
        """Return the value of the sensor."""
        if isinstance(self._current_value, (str, int)):
            return self._current_value

        return None

    @property
    def current_value(self) -> Any:
        """Return the current value of the channel."""
        return self._current_value

    def _subscribe(self) -> None:
        channel_names = {self.component.name + "/" + self.name}
        self.component.edge.register_channel(channel_names, self)

    def _unsubscribe(self) -> None:
        if self._rest_update_task is not None:
            self._rest_update_task.cancel()
            self._rest_update_task = None
        self.component.edge.unregister_channel(self)

    async def write_via_rest(
        self, new_value: float | bool, update_cycle: int, timeout: int
    ) -> None:
        """Write a channel value via the REST API of the edge.

        With update_cycle > 0 the value is written cyclically until timeout seconds
        passed (0: no timeout) or another write of the channel starts.
        """
        edge = self.component.edge
        backend = edge.backend
        # Note: This is not a component property.
        # This means the value change request must be sent via REST.
        if not edge.rest or edge.rest.mode != "ReadWrite":
            _LOGGER.error(
                "Cannot update channel %s/%s: No REST write app found",
                self.component.name,
                self.name,
            )
            return

        # clean up a previously running cyclic update task
        if self._rest_update_task is not None:
            self._rest_update_task.cancel()
            self._rest_update_task = None

        # prepare REST URL and data
        url = edge.rest.url.joinpath("channel", self.component.name, self.name)
        data = {"value": new_value}

        async def _rest_update_cyclic() -> None:
            """Send REST update command cyclically."""
            while True:
                await _rest_update_oneshot()
                await asyncio.sleep(update_cycle)

        async def _rest_post(session: aiohttp.ClientSession) -> None:
            auth = aiohttp.BasicAuth(
                backend.connection.username, backend.connection.password
            )
            async with session.post(url, json=data, auth=auth) as resp:
                if resp.status != 200:
                    _LOGGER.error(
                        "Error during REST call to update channel %s/%s: HTTP %d: %s",
                        self.component.name,
                        self.name,
                        resp.status,
                        resp.reason,
                    )

        async def _rest_update_oneshot() -> None:
            """Send REST update command."""
            try:
                if (session := backend.connection.session) is not None:
                    await _rest_post(session)
                else:
                    async with aiohttp.ClientSession() as own_session:
                        await _rest_post(own_session)
            except aiohttp.ClientError as exc:
                _LOGGER.error(
                    "Cannot send REST update command via URL %s for channel %s/%s: %s",
                    str(url),
                    self.component.name,
                    self.name,
                    str(exc),
                )

        if update_cycle <= 0:
            # one shot
            await _rest_update_oneshot()
        else:
            # schedule cyclically
            my_timeout = timeout if timeout > 0 else None
            self._rest_update_task = asyncio.create_task(_rest_update_cyclic())
            try:
                await asyncio.wait_for(self._rest_update_task, timeout=my_timeout)
            except TimeoutError:
                self._rest_update_task = None
            except asyncio.CancelledError:
                # another update task took over. Dont touch the task reference.
                pass

        _LOGGER.info(
            "Update value service task completed for channel %s/%s",
            self.component.name,
            self.name,
        )


type DerivedSourceValue = str | float | None
type DerivedValueFn = Callable[
    [Mapping[str, DerivedSourceValue | list[DerivedSourceValue]]], float | None
]


@dataclass(frozen=True, kw_only=True)
class DerivedChannelDefinition:
    """Definition of a channel whose value is calculated from other channels.

    `id` and the `sources` channel id patterns may contain {name} placeholders.
    A placeholder in `id` creates one derived channel per matching value. A
    placeholder that only occurs in a source turns it into a list of all
    matching channels. `value_fn` receives the source values by source name
    and is only called when all of them are known.
    """

    id: str
    sources: Mapping[str, str]
    value_fn: DerivedValueFn
    unit: str


class OpenEMSDerivedChannel(OpenEMSDataHandler):
    """Class representing a derived sensor of an OpenEMS component."""

    def __init__(
        self,
        component: OpenEMSComponent,
        name: str,
        unit: str,
        sources: Mapping[str, str | list[str]],
        value_fn: DerivedValueFn,
    ) -> None:
        """Initialize the derived channel."""
        super().__init__(component, name)
        self.unit: str = unit
        self._sources = sources
        self._value_fn = value_fn
        channel_ids: list[str] = []
        for source in sources.values():
            channel_ids.extend(source if isinstance(source, list) else [source])
        self.reference_channels: dict[str, DerivedSourceValue] = dict.fromkeys(
            component.name + "/" + channel_id for channel_id in channel_ids
        )

    def _subscribe(self) -> None:
        channel_names = set(self.reference_channels) | {
            self.component.name + "/" + self.name,
        }
        self.component.edge.register_channel(channel_names, self)

    def _unsubscribe(self) -> None:
        self.component.edge.unregister_channel(self)

    def _source_value(
        self, source: str | list[str]
    ) -> DerivedSourceValue | list[DerivedSourceValue]:
        if isinstance(source, list):
            return [
                self.reference_channels[self.component.name + "/" + channel_id]
                for channel_id in source
            ]
        return self.reference_channels[self.component.name + "/" + source]

    def _calculate(self) -> float | None:
        values = {name: self._source_value(src) for name, src in self._sources.items()}
        for value in values.values():
            if value is None or (isinstance(value, list) and None in value):
                return None
        try:
            result = self._value_fn(values)
            return None if result is None else float(result)
        except ValueError, TypeError:
            return None

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        if channel_name in self.reference_channels:
            if self.reference_channels[channel_name] != value:
                self.reference_channels[channel_name] = value

                channel_value = self._calculate()
                if channel_value != self._current_value:
                    self._current_value = channel_value
                    self._notify_listeners()
            return

    @property
    def value(self) -> float | None:
        """Return the value of the derived channel."""
        if isinstance(self._current_value, (float, int)):
            return self._current_value

        return None


class OpenEMSProperty(OpenEMSChannel):
    """Class representing a property of an OpenEMS component."""

    @property
    def config_name(self) -> str:
        """Return the name of the property in the component configuration."""
        # remove "_Property" prefix and lower the first character
        return self.name[9].lower() + self.name[10:]

    def to_backend_value(self, value: Any) -> Any:
        """Convert a value in Home Assistant representation to the backend representation.

        Raise ValueError if the value is invalid.
        """
        return value

    async def update_value(self, new_value: Any) -> None:
        """Handle value change request from Home Assistant."""
        await self.component.update_properties({self: new_value})


class OpenEMSEnumProperty(OpenEMSProperty):
    """Class representing a enum property of an OpenEMS component."""

    # Use with platform select
    def __init__(
        self,
        component: OpenEMSComponent,
        channel_json: dict[str, Any],
        options: list[str],
    ) -> None:
        """Initialize the channel."""
        super().__init__(component, channel_json)
        self.property_options: list[str] = options

    def to_backend_value(self, value: Any) -> str:
        """Convert the snake_case or original option to the original option."""
        for option in self.property_options:
            if value in (option, to_snake_case(option)):
                return option
        raise ValueError(f"Invalid option: {value}")

    @property
    def current_option(self) -> str | None:
        """Return the current option."""
        if isinstance(self._current_value, str):
            return self._current_value

        return None

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        if value is None:
            self.handle_current_value(None)
        elif isinstance(value, str):
            if value in self.property_options:
                self.handle_current_value(value)
            else:
                _LOGGER.warning(
                    "Received unknown value option for %s/%s: %s. Setting to status to Unknown",
                    self.name,
                    self.component.name,
                    value,
                )
                self.handle_current_value(None)
        else:
            self.handle_current_value(value)


class OpenEMSTimeProperty(OpenEMSProperty):
    """Class representing a time property of an OpenEMS component."""

    # Use with platform time
    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        if not isinstance(value, str):
            new_val = None
        else:
            try:
                hour_str, minute_str = value.split(":")
                new_val = time(int(hour_str), int(minute_str))
            except ValueError:
                new_val = None
        self.handle_current_value(new_val)

    @property
    def value(self) -> time | None:
        """Return the value of the time entity."""
        if isinstance(self._current_value, time):
            return self._current_value

        return None

    def to_backend_value(self, value: Any) -> str:
        """Convert a time object or a "HH:MM[:SS]" string to "HH:MM"."""
        if isinstance(value, time):
            return value.strftime("%H:%M")
        try:
            return time.fromisoformat(value).strftime("%H:%M")
        except (TypeError, ValueError) as err:
            raise ValueError(f"Invalid time: {value}") from err


class OpenEMSNumberProperty(OpenEMSProperty):
    """Class representing a number property of an OpenEMS component."""

    STEPS = 200  # Minimum number of steps

    # Use with platform Number
    def __init__(
        self,
        component: OpenEMSComponent,
        channel_json: dict[str, Any],
    ) -> None:
        """Initialize the number channel."""
        super().__init__(component, channel_json)
        self.multiplier: float = 1.0
        self.lower_limit: float = 0
        self.upper_limit: float = 100000

        self.multiplier_def: Template = Template(str(self.multiplier))
        self.lower_limit_def: Template = Template(str(self.lower_limit))
        self.upper_limit_def: Template = Template(str(self.upper_limit))

        self.step: float = 1.0
        # fixed step size from the config, replaces the calculated step size
        self.fixed_step: float | None = None
        self.reference_channels: dict[str, str | float | None] = {}

    @property
    def value(self) -> float | None:
        """Return the value of the number property."""
        if isinstance(self._current_value, (float, int)):
            return self._current_value

        return None

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        channel_reference = channel_name.replace("/", SLASH_ESC)
        if channel_reference in self.reference_channels:
            if self.reference_channels[channel_reference] != value:
                self.reference_channels[channel_reference] = value
                if self._update_config():
                    # config vars changed. Update the entity in HA
                    self._notify_listeners()
            return

        if isinstance(value, (float, int)):
            new_val = self.multiplier * value
        else:
            new_val = None
        self.handle_current_value(new_val)

    def _update_config(self) -> bool:
        """Calculate the new multiplier, limits and step after references changed.

        Return True if at least one of these parameters changed, False otherwise.
        """
        multiplier = self.multiplier
        lower_limit = self.lower_limit
        upper_limit = self.upper_limit
        step = self.step
        if self.multiplier_def:
            try:
                render_result = self.multiplier_def.render(self.reference_channels)
                multiplier = float(render_result)
            except ValueError, TypeError:
                multiplier = 1.0

        try:
            render_result = self.lower_limit_def.render(self.reference_channels)
            lower_noscale = float(render_result)
        except ValueError, TypeError:
            lower_noscale = None

        try:
            render_result = self.upper_limit_def.render(self.reference_channels)
            upper_noscale = float(render_result)
        except ValueError, TypeError:
            upper_noscale = None

        if upper_noscale is None or lower_noscale is None:
            lower_limit = 0
            upper_limit = 100000
        else:
            # assure upper limit is larger than lower limit
            if upper_noscale <= lower_noscale:
                upper_noscale = lower_noscale + 10
                _LOGGER.warning(
                    "Upper limit of %s is too small, resulting from config. Adjusting to %d",
                    self.name,
                    upper_noscale,
                )
            lower_scaled = lower_noscale * multiplier
            upper_scaled = upper_noscale * multiplier
            if self.fixed_step is not None:
                step = self.fixed_step
            else:
                # split range into 200, but assure min step size of 1
                min_step_range = max(
                    1, (upper_scaled - lower_scaled) / OpenEMSNumberProperty.STEPS
                )
                # align step size with a power of 10
                step = 10 ** math.ceil(math.log10(min_step_range))
            lower_limit = math.ceil(float(lower_scaled) / step) * step
            upper_limit = math.ceil(float(upper_scaled) / step) * step

        if (new_config := (multiplier, lower_limit, upper_limit, step)) != (
            self.multiplier,
            self.lower_limit,
            self.upper_limit,
            self.step,
        ):
            self.multiplier, self.lower_limit, self.upper_limit, self.step = new_config
            _LOGGER.debug(
                "Config of entity %s changed: Multiplier: %d, Lower Limit: %d, Upper Limit: %d, Step: %d",
                self.name,
                self.multiplier,
                self.lower_limit,
                self.upper_limit,
                self.step,
            )
            return True

        return False

    def to_backend_value(self, value: Any) -> float:
        """Convert the scaled Home Assistant value to the unscaled backend value."""
        if isinstance(value, bool):
            # ValueError, as all invalid user input is reported as ValueError
            raise ValueError(f"Invalid number: {value}")  # noqa: TRY004
        try:
            return float(value) / self.multiplier
        except (TypeError, ValueError) as err:
            raise ValueError(f"Invalid number: {value}") from err

    async def update_value(self, new_value: float) -> None:
        """Handle value change request from Home Assistant."""
        await super().update_value(self.to_backend_value(new_value))

    def set_multiplier_def(self, multiplier_def: str) -> None:
        """Initialize the multiplier of the number channel."""
        self.multiplier_def, multiplier_references = prepare_ref_value(
            multiplier_def, self.component
        )
        if not multiplier_references:
            # no external references. Calculate the result immediately
            self.multiplier = float(self.multiplier_def.render())
        else:
            for ref in multiplier_references:
                self.reference_channels[ref] = None

    def set_step(self, step: float) -> None:
        """Initialize a fixed step size of the number channel."""
        self.fixed_step = step
        self.step = step

    def set_limit_def(self, limit_def: dict[str, str]) -> None:
        """Initialize the limits of the number channel."""
        self.lower_limit_def, lower_references = prepare_ref_value(
            limit_def["lower"], self.component
        )
        if not lower_references:
            # no external references. Calculate the result immediately
            self.lower_limit = float(self.lower_limit_def.render())
        else:
            for ref in lower_references:
                self.reference_channels[ref] = None

        self.upper_limit_def, upper_references = prepare_ref_value(
            limit_def["upper"], self.component
        )
        if not upper_references:
            # no external references. Calculate the result immediately
            self.upper_limit = float(self.upper_limit_def.render())
        else:
            for ref in upper_references:
                self.reference_channels[ref] = None

        if not (lower_references or upper_references):
            self._update_config()

    def _subscribe(self) -> None:
        channel_names = {x.replace(SLASH_ESC, "/") for x in self.reference_channels} | {
            self.component.name + "/" + self.name,
        }
        self.component.edge.register_channel(channel_names, self)


class OpenEMSBooleanProperty(OpenEMSProperty):
    """Class representing a boolean property of an OpenEMS component."""

    # Use with platform switch
    TRUE_VALUES = ("1", "true", "on", "yes", "enable")
    FALSE_VALUES = ("0", "false", "off", "no", "disable")

    def to_backend_value(self, value: Any) -> bool:
        """Convert booleans, 0/1 and strings like "on"/"off" to bool."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            if value.lower() in self.TRUE_VALUES:
                return True
            if value.lower() in self.FALSE_VALUES:
                return False
        raise ValueError(f"Invalid boolean: {value}")

    @property
    def is_on(self) -> bool | None:
        """Return true if switch is on."""
        is_on: bool | None = self._current_value
        return is_on

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        if isinstance(value, int):
            new_val = bool(value)
        else:
            new_val = None
        self.handle_current_value(new_val)


class OpenEMSComponent:
    """Class representing a component of an OpenEMS Edge."""

    def __init__(self, edge: OpenEMSEdge, name: str, json_def: dict[str, Any]) -> None:
        """Initialize the component."""
        self.edge: OpenEMSEdge = edge
        self.name: str = name
        self.alias: str | None = json_def.get("_PropertyAlias")
        self.ref_values: dict[str, Any] = {}
        self.json_properties: dict[str, Any] = json_def["properties"]
        self.sensors: list[OpenEMSChannel] = []
        self.boolean_sensors: list[OpenEMSChannel] = []
        self.enum_properties: list[OpenEMSEnumProperty] = []
        self.number_properties: list[OpenEMSNumberProperty] = []
        self.boolean_properties: list[OpenEMSBooleanProperty] = []
        self.time_properties: list[OpenEMSTimeProperty] = []
        self.channel_ids: list[str] = []

    def init_channels(self, channels: list[dict[str, Any]]) -> None:
        """Parse and initialize the components channels."""
        self.channel_ids = [c["id"] for c in channels]
        for channel_json in channels:
            options_backend: dict[str, int] | list[str] | None = channel_json.pop(
                "options", None
            )
            if channel_json["id"].startswith("_Property"):
                # scan type and convert to property
                match channel_json["type"]:
                    case "BOOLEAN":
                        boolean_prop = OpenEMSBooleanProperty(
                            component=self, channel_json=channel_json
                        )
                        self.boolean_properties.append(boolean_prop)
                    case "STRING":
                        options = (
                            # options received from backend are preferred over configured options
                            options_backend
                            if isinstance(options_backend, list)
                            else CONFIG.get_enum_options(self.name, channel_json["id"])
                        )
                        if options is not None:
                            enum_prop = OpenEMSEnumProperty(
                                component=self,
                                channel_json=channel_json,
                                options=options,
                            )
                            self.enum_properties.append(enum_prop)
                        elif CONFIG.is_time_property(self.name, channel_json["id"]):
                            time_prop = OpenEMSTimeProperty(
                                component=self, channel_json=channel_json
                            )
                            self.time_properties.append(time_prop)
                    case "INTEGER":
                        if limit_def := CONFIG.get_number_limit(
                            self.name, channel_json["id"]
                        ):
                            try:
                                multiplier = CONFIG.get_number_multiplier(
                                    self.name, channel_json["id"]
                                )
                                number_prop = OpenEMSNumberProperty(
                                    component=self, channel_json=channel_json
                                )
                                if multiplier is not None:
                                    number_prop.set_multiplier_def(multiplier)
                                if (
                                    step := CONFIG.get_number_step(
                                        self.name, channel_json["id"]
                                    )
                                ) is not None:
                                    number_prop.set_step(step)
                                number_prop.set_limit_def(limit_def)
                                self.number_properties.append(number_prop)
                            except (
                                TypeError,
                                jsonrpc_base.jsonrpc.TransportError,
                                jsonrpc_base.jsonrpc.ProtocolError,
                                ValueError,
                            ):
                                _LOGGER.warning(
                                    "Error during initialization of channel %s/%s",
                                    self.name,
                                    channel_json["id"],
                                    exc_info=True,
                                )

            else:
                channel_options = (
                    options_backend if isinstance(options_backend, dict) else None
                )
                channel = OpenEMSChannel(
                    component=self, channel_json=channel_json, options=channel_options
                )
                match channel_json["type"]:
                    case "BOOLEAN":
                        self.boolean_sensors.append(channel)
                    case _:
                        self.sensors.append(channel)

    def create_derived_channels(
        self, definitions: Iterable[DerivedChannelDefinition]
    ) -> list[OpenEMSDerivedChannel]:
        """Create the derived channels for the given definitions."""
        return [
            OpenEMSDerivedChannel(
                self, name, definition.unit, sources, definition.value_fn
            )
            for definition in definitions
            for name, sources in expand_derived_definition(definition, self.channel_ids)
        ]

    def get_property(self, name: str) -> OpenEMSProperty | None:
        """Return the property with the given channel name or config name."""
        return next(
            (prop for prop in self.properties if name in (prop.name, prop.config_name)),
            None,
        )

    async def update_properties(self, values: dict[OpenEMSProperty, Any]) -> None:
        """Send the given backend values in a single updateComponentConfig request.

        Members of an update group, which are not part of values, are added with their current value.
        """
        implied: dict[OpenEMSProperty, Any] = {}
        for prop, value in values.items():
            channel_names, condition_value = CONFIG.update_group_members(
                self.name, prop.name
            )
            if condition_value is None or condition_value == value:
                for channel_name in channel_names:
                    member = self.get_property(channel_name)
                    if member is not None and member not in values:
                        implied.setdefault(member, member.current_value)

        await self.update_config(
            [(prop.config_name, value) for prop, value in {**values, **implied}.items()]
        )

    async def update_config(self, channels: list[tuple[str, Any]]) -> None:
        """Send updateComponentConfig request to backend."""
        properties = [{"name": chan[0], "value": chan[1]} for chan in channels]
        if not self.edge.backend.connection.rpc_server.connected:
            raise OpenEMSConnectionError(
                f"Not connected to the backend, cannot update component {self.name}"
            )

        envelope = wrap_jsonrpc(
            "updateComponentConfig", componentId=self.name, properties=properties
        )
        _LOGGER.debug(
            "updateComponentConfig: component: %s, properties: %s",
            self.name,
            str(properties),
        )
        await backend_call(
            self.edge.backend.connection.rpc_server.edgeRpc(
                edgeId=self.edge.id, payload=envelope
            )
        )

    @property
    def channels(self) -> list[OpenEMSDataHandler]:
        """Return all channels of the component (all platforms)."""
        return [
            *self.properties,
            *self.sensors,
            *self.boolean_sensors,
        ]

    @property
    def properties(self) -> list[OpenEMSProperty]:
        """Return all properties of the component (all platforms)."""
        return [
            *self.enum_properties,
            *self.number_properties,
            *self.boolean_properties,
            *self.time_properties,
        ]


class OpenEMSEdge:
    """Class representing an OpenEMS Edge device."""

    class OpenEmsEdgeChannelSubscriptionUpdater:
        """Allows to register callbacks methods and get notified on updates."""

        def __init__(self, edge: OpenEMSEdge) -> None:
            """Initialize the updater."""
            self._edge: OpenEMSEdge = edge
            self._fetch_task: asyncio.Task[None] | None = None
            self._active_subscriptions: list[str] = []
            self._count = 0

        def start(self) -> None:
            """Start the updater."""
            self._fetch_task = asyncio.create_task(self._update_subscriptions_forever())

        async def stop(self) -> None:
            """Stop the updater and wait until it has finished."""
            await cancel_task(self._fetch_task)
            self._fetch_task = None

        def clear(self) -> None:
            """Clear the list of active subscriptions."""
            self._active_subscriptions = []

        async def _update_subscriptions_forever(self) -> None:
            try:
                _LOGGER.debug("SubscriptionUpdater start")
                while True:
                    await asyncio.sleep(5)
                    subscribe_in_progress_channels = list(
                        self._edge.registered_channels.keys()
                    )
                    if (
                        self._edge.backend.connection.rpc_server.connected
                        and subscribe_in_progress_channels != self._active_subscriptions
                    ):
                        try:
                            if not self._active_subscriptions:
                                # no active subscription, so subscribe for the edge
                                await self._edge.backend.connection.rpc_server.subscribeEdges(
                                    edges=[self._edge.id]
                                )
                                self._count = 0
                            else:
                                self._count += 1

                            subscribe_call = wrap_jsonrpc(
                                "subscribeChannels",
                                count=self._count,
                                channels=subscribe_in_progress_channels,
                            )
                            await self._edge.backend.connection.rpc_server.edgeRpc(
                                edgeId=self._edge.id, payload=subscribe_call
                            )
                            self._active_subscriptions = subscribe_in_progress_channels
                            _LOGGER.debug(
                                "SubscriptionUpdater update: %d entities",
                                len(subscribe_in_progress_channels),
                            )
                        except (
                            jsonrpc_base.jsonrpc.TransportError,
                            jsonrpc_base.jsonrpc.ProtocolError,
                        ):
                            _LOGGER.exception(
                                "SubscriptionUpdater error during subscribe"
                            )
            except asyncio.CancelledError:
                _LOGGER.debug("SubscriptionUpdater end")
                raise

    def __init__(
        self,
        backend: OpenEMSBackend,
        id: str,
        component_config: dict[str, dict[str, Any]],
    ) -> None:
        """Initialize the edge."""
        self.backend: OpenEMSBackend = backend
        self._id: str = id
        # 0 forwards channel data immediately, >0 at most every N seconds
        self._forward_interval: int = 0
        self._channel_data_forwarder: asyncio.Task[None] | None = None
        self._started: bool = False
        self.current_channel_data: dict[str, Any] = {}
        self._channel_subscription_updater = self.OpenEmsEdgeChannelSubscriptionUpdater(
            self
        )
        self._registered_handlers: dict[str, set[OpenEMSDataHandler]] = {}
        self.hostname: str = component_config["_host"]["Hostname"]
        if self.backend.multi_edge:
            self.hostname += " " + self.id

        self.components: dict[str, OpenEMSComponent] = self._prepare_entities(
            component_config
        )
        self.rest: RestDetails | None = self._prepare_rest(component_config)

    def _prepare_entities(
        self, component_config: dict[str, dict[str, Any]]
    ) -> dict[str, OpenEMSComponent]:
        """Parse json config and create class structures."""
        components: dict[str, OpenEMSComponent] = {}
        for name, contents in component_config.items():
            if "channels" in contents:
                component: OpenEMSComponent = OpenEMSComponent(self, name, contents)
                component.init_channels(contents["channels"])
                # create the entities within a service which linked to the edge device
                components[name] = component
        return components

    def _prepare_rest(
        self, component_config: dict[str, dict[str, Any]]
    ) -> RestDetails | None:
        # Fenecon Web portal does not support REST API access
        if (
            self.backend.connection.conn_url.host
            == CONN_TYPES[CONN_TYPE_WEB_FENECON]["host"]
        ):
            return None
        for comp_content in component_config.values():
            if comp_content.get("factoryId", "").startswith("Controller.Api.Rest"):
                rest_mode = comp_content["factoryId"][20:]
                rest_url = connection_url(
                    CONN_TYPE_REST, self.backend.connection.conn_url.host
                )
                rest_url = rest_url.with_port(
                    int(comp_content["properties"].get("port", rest_url.port))
                )
                return RestDetails(rest_mode, rest_url)
        # No REST App found
        return None

    def set_unavailable(self) -> None:
        """Set all active entities to unavailable and clear the subscription indicator."""
        # Note: This method is called by the connection logic on connection loss.
        for channel_name in self.current_channel_data:
            for handler in self._registered_handlers[channel_name]:
                handler.handle_data_update(channel_name, None)
        self._channel_subscription_updater.clear()

    def start(self) -> None:
        """Start the subscription updates and the channel data forwarder."""
        self._started = True
        self._channel_subscription_updater.start()
        self._start_channel_data_forwarder()

    async def stop(self) -> None:
        """Stop all subscriptions and wait until the background tasks have finished."""
        self._started = False
        await self._channel_subscription_updater.stop()
        await cancel_task(self._channel_data_forwarder)
        self._channel_data_forwarder = None

    def _start_channel_data_forwarder(self) -> None:
        if self._forward_interval:
            self._channel_data_forwarder = asyncio.create_task(
                self._forwarder_channel_data_forever(self._forward_interval)
            )

    def edgeConfig(self, params: dict[str, Any]) -> None:
        """Jsonrpc callback to receive edge config updates."""
        # TODO: renew component channels and component info values
        # self._prepare_entities(params["components"])

    def _forward_current_channel_data(self, params: dict[str, BackendValue]) -> None:
        """Forward channel data to registered handlers."""
        for channel_name, value in params.items():
            registered_handlers = self._registered_handlers.get(channel_name)
            if not registered_handlers:
                _LOGGER.debug(
                    "Received data update for unsubscribed channel: %s", channel_name
                )
                continue
            for handler in registered_handlers:
                handler.handle_data_update(channel_name, value)

    async def _forwarder_channel_data_forever(self, interval: int) -> None:
        """Periodically forward the latest channel data snapshot to registered handlers."""
        while True:
            await asyncio.sleep(interval)
            self._forward_current_channel_data(self.current_channel_data)

    def currentData(self, params: dict[str, BackendValue]) -> None:
        """Jsonrpc callback to receive channel subscription updates."""
        self.backend.connection.notify_data_received()
        self.current_channel_data = params

        # forward data only when there is no forwarder, meaning no forward interval is set.
        if self._channel_data_forwarder is None:
            self._forward_current_channel_data(params)

    def register_channel(
        self, channel_names: set[str], handler: OpenEMSDataHandler
    ) -> None:
        """Register a channel and its dependent channels for updates."""
        for channel_name in channel_names:
            if (
                isinstance(handler, OpenEMSChannel)
                and access_mode(handler) & OpenEMSAccessMode.READ == 0
            ):
                _LOGGER.info(
                    "Skipping subscription for channel %s without READ access",
                    channel_name,
                )
                continue
            if channel_name not in self._registered_handlers:
                self._registered_handlers[channel_name] = {handler}
            else:
                self._registered_handlers[channel_name].add(handler)

    def unregister_channel(self, handler: OpenEMSDataHandler) -> None:
        """Remove a channel from receiving updates."""
        for channel_name, handlers in list(self._registered_handlers.items()):
            if handler in handlers:
                handlers.remove(handler)
                if not handlers:
                    del self._registered_handlers[channel_name]

    @property
    def id(self) -> str:
        """Return the edge ID."""
        return self._id

    @property
    def registered_channels(self) -> dict[str, set[OpenEMSDataHandler]]:
        "Return the list of all subscribed channels."
        return self._registered_handlers

    async def get_system_update_state(self) -> dict[str, Any]:
        """Read getSystemUpdateState response."""
        system_update_state_call = wrap_jsonrpc("getSystemUpdateState")
        edge_call = wrap_jsonrpc(
            "componentJsonApi", componentId="_host", payload=system_update_state_call
        )
        result = await backend_call(
            self.backend.connection.rpc_server.edgeRpc(
                edgeId=self._id, payload=edge_call
            )
        )
        update_state: dict[str, Any] = result["payload"]["result"]
        return update_state

    async def execute_system_update(self) -> None:
        """Trigger executeSystemUpdate request."""
        system_update_state_call = wrap_jsonrpc("executeSystemUpdate", isDebug=False)
        edge_call = wrap_jsonrpc(
            "componentJsonApi", componentId="_host", payload=system_update_state_call
        )
        # the update runs for minutes, the caller tracks it via get_system_update_state
        await backend_call(
            self.backend.connection.rpc_server.edgeRpc(
                edgeId=self._id, payload=edge_call
            ),
            timeout=None,
        )

    async def execute_system_restart(self) -> None:
        """Trigger executeSystemRestart request (hard restart of the edge)."""
        system_restart_call = wrap_jsonrpc("executeSystemRestart", type="HARD")
        edge_call = wrap_jsonrpc(
            "componentJsonApi", componentId="_host", payload=system_restart_call
        )
        await backend_call(
            self.backend.connection.rpc_server.edgeRpc(
                edgeId=self._id, payload=edge_call
            )
        )

    def set_forward_interval(self, seconds: int) -> None:
        """Forward channel data at most every N seconds, 0 forwards it immediately."""
        old_interval = self._forward_interval
        self._forward_interval = seconds
        if seconds != old_interval and self._started:
            if self._channel_data_forwarder is not None:
                self._channel_data_forwarder.cancel()
                self._channel_data_forwarder = None
            self._start_channel_data_forwarder()


class OpenEMSBackend:
    """Class which represents a connection to an OpenEMS backend."""

    def __init__(
        self,
        connection: OpenEMSWebSocketConnection,
        edge_id: str,
        multi_edge: bool,
        components: dict[str, dict[str, Any]],
    ) -> None:
        """Create a new OpenEMSBackend object."""
        self.connection: OpenEMSWebSocketConnection = connection
        self.multi_edge: bool = multi_edge

        connection.rpc_server.edgeRpc = self.edgeRpc
        self.the_edge = OpenEMSEdge(self, edge_id, components)

    def edgeRpc(self, **kwargs: Any) -> None:
        """Handle an edge jsonrpc callback and call the respective method of the edge object."""
        if kwargs["edgeId"] != self.the_edge.id:
            _LOGGER.error("Received response for undefined edge: %s", kwargs["edgeId"])
            return

        method_name = kwargs["payload"]["method"]
        try:
            method = getattr(self.the_edge, method_name)
        except AttributeError:
            _LOGGER.error("Unhandled callback method: %s", method_name)
            return

        params = kwargs["payload"]["params"]
        # call the edge method
        method(params)

    def start(self) -> None:
        """Start the reconnect logic and the background tasks of the edge."""
        self.connection.enable_reconnect(self.the_edge.set_unavailable)
        self.the_edge.start()

    async def stop(self) -> None:
        """Close the connection and wait until all background tasks have finished."""
        await self.connection.stop()
        await self.the_edge.stop()
