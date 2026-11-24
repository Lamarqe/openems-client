"""OpenEMS API."""

from abc import abstractmethod
import asyncio
from collections.abc import Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from datetime import time
import logging
import math
import re
from typing import Any, NamedTuple, cast, overload

import aiohttp
import jsonrpc_base
from yarl import URL

from .config import OpenEMSConfig
from .connection import OpenEMSWebSocketConnection, backend_call
from .const import (
    CONN_TYPE_REST,
    CONN_TYPE_WEB_FENECON,
    CONN_TYPES,
    REQUEST_TIMEOUT_SECONDS,
    SLASH_ESC,
)
from .exceptions import OpenEMSConnectionError, OpenEMSError, OpenEMSTimeoutError
from .fems import (
    FEMS_CHANGELOG_URL,
    FENECON,
    ChangelogRelease,
    is_fems,
    parse_changelog,
    release_notes,
    release_url,
    releases_between,
)
from .helpers import cancel_task, connection_url, to_bool, to_snake_case, wrap_jsonrpc
from .helpers_openems import (
    NumberExpression,
    OpenEMSAccessMode,
    access_mode,
    expand_derived_definition,
)
from .model import (
    ChannelKind,
    Quantity,
    Severity,
    channel_key,
    display_name,
    severity,
    unit_info,
)
from .system_update import SystemUpdateState, UpdateStatus, parse_system_update_state

_LOGGER = logging.getLogger(__name__)


class RestDetails(NamedTuple):
    """Details about the REST connection."""

    mode: str
    url: URL


CONFIG: OpenEMSConfig = OpenEMSConfig()

META_VERSION_CHANNEL = "_meta/Version"

# value of a channel as received from the backend
type BackendValue = str | int | float | None
# value of a channel as provided by the channel objects
type ChannelValue = str | int | float | bool | time | None


class OpenEMSDataHandler:
    """Interface for handling data updates from the backend."""

    kind: ChannelKind

    def __init__(
        self, component: OpenEMSComponent, name: str, unit: str | None = None
    ) -> None:
        """Initialize the handler."""
        self.component: OpenEMSComponent = component
        self.name: str = name
        # stable snake_case id of the channel, shared by all components
        self.key: str = channel_key(name)
        # readable English name, eg "Max DC charge power" for "MaxDcChargePower"
        self.display_name: str = display_name(name, CONFIG.acronyms)
        info = unit_info(unit, self.key)
        self.unit: str | None = info.unit
        self.quantity: Quantity | None = info.quantity
        self.cumulative: bool = info.cumulative
        # a measured value with a unit, eg power; numbers without unit are codes, IDs or settings
        self.measurement: bool = info.unit is not None and not info.cumulative
        self._divisor: int = info.divisor
        # one of the main channels of the component, see config/primary_channels.json
        self.primary: bool = CONFIG.is_primary_channel(component.name, name)
        self.severity: Severity | None = None
        # snake_case keys of the possible values of enum channels
        self.option_keys: list[str] | None = None
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

    @property
    def available(self) -> bool:
        """Return True while the edge is connected and sends channel data."""
        return self.component.edge.available

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

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the channel."""
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
    """Class representing a read-only channel of an OpenEMS component."""

    def __init__(
        self,
        component: OpenEMSComponent,
        channel_json: dict[str, Any],
        options: dict[str, int] | None = None,
    ) -> None:
        """Initialize the channel."""
        super().__init__(component, channel_json["id"], channel_json["unit"])
        # backend value -> option name
        self.options: dict[int, str] | None = None
        self._option_keys_by_value: dict[int, str] = {}
        if channel_json.get("category") == "ENUM" and options is not None:
            self.options = {v: k for k, v in options.items()}
            self._option_keys_by_value = {
                v: to_snake_case(k) for k, v in options.items()
            }
            self.option_keys = list(dict.fromkeys(self._option_keys_by_value.values()))
        self.kind = self._channel_kind(channel_json)
        if self.kind is ChannelKind.BINARY_SENSOR:
            self.severity = severity(channel_json.get("level"))
        self.orig_json: dict[str, Any] = channel_json
        self._rest_update_task: asyncio.Task[None] | None = None

    def _channel_kind(self, channel_json: dict[str, Any]) -> ChannelKind:
        if channel_json.get("accessMode") == "WO":
            return ChannelKind.WRITE_ONLY
        if channel_json["type"] == "BOOLEAN":
            return ChannelKind.BINARY_SENSOR
        if self.options is not None:
            return ChannelKind.ENUM_SENSOR
        if channel_json["type"] == "STRING":
            return ChannelKind.TEXT_SENSOR
        return ChannelKind.SENSOR

    def handle_current_value(self, value: Any) -> None:
        """Handle a new entity value and notify Home Assistant."""
        if value != self._current_value:
            self._current_value = value
            self._notify_listeners()

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        """Handle a data update from the backend."""
        match self.kind:
            case ChannelKind.ENUM_SENSOR if isinstance(value, int):
                self.handle_current_value(self._option_keys_by_value.get(value))
            case ChannelKind.BINARY_SENSOR if isinstance(value, int):
                self.handle_current_value(bool(value))
            case ChannelKind.TEXT_SENSOR if isinstance(value, str):
                self.handle_current_value(value)
            case ChannelKind.SENSOR if isinstance(value, int | float):
                self.handle_current_value(
                    value / self._divisor if self._divisor != 1 else value
                )
            case _:
                self.handle_current_value(None)

    @property
    def value(self) -> ChannelValue:
        """Return the value: a number, the option key of enums, a bool of binary sensors.

        Text sensors provide the text.
        """
        if isinstance(self._current_value, (str, int, float)):
            return self._current_value

        return None

    @property
    def is_on(self) -> bool | None:
        """Return the state of a binary sensor."""
        if isinstance(self._current_value, bool):
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
        self.component.edge.unregister_channel(self)

    def to_rest_value(self, value: Any) -> str | bool | int | float:
        """Convert a value to the type of the channel, raise ValueError if invalid.

        Numbers are given in the normalised unit, eg °C for channels in °dC.
        """
        channel_type = self.orig_json["type"]
        if channel_type == "STRING":
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                raise ValueError(f"Invalid text: {value}")
            return str(value)
        if channel_type == "BOOLEAN":
            return to_bool(value)
        if isinstance(value, bool):
            # ValueError, as all invalid user input is reported as ValueError
            raise ValueError(f"Invalid number: {value}")  # noqa: TRY004
        try:
            number = float(value) * self._divisor
        except (TypeError, ValueError) as err:
            raise ValueError(f"Invalid number: {value}") from err
        if channel_type in ("FLOAT", "DOUBLE"):
            return number
        if not number.is_integer():
            raise ValueError(f"Invalid integer: {value}")
        return int(number)

    async def write_via_rest(
        self, value: Any, update_cycle: float = 0, timeout: float = 0
    ) -> None:
        """Write the value of a write-only channel via the REST API of the edge.

        Raise ValueError if the channel is not write-only or the value does not fit
        the channel, OpenEMSError if the edge has no REST app with write access or
        rejects the value, OpenEMSConnectionError if the REST API cannot be reached.

        With update_cycle > 0 the value is written again every update_cycle seconds
        in the background, until timeout seconds passed (0: no timeout), another
        write of the channel starts or the edge stops. Errors of the first write are
        raised, errors of the repeated writes are logged.
        """
        if self.kind is not ChannelKind.WRITE_ONLY:
            raise ValueError(
                f"Channel {self.component.name}/{self.name} is not write-only"
            )
        data = {"value": self.to_rest_value(value)}
        edge = self.component.edge
        if not edge.rest_writable or edge.rest is None:
            raise OpenEMSError(
                f"Cannot write channel {self.component.name}/{self.name}: "
                "no REST app with write access"
            )

        # a new write replaces a running cyclic write of the channel
        await cancel_task(self._rest_update_task)
        self._rest_update_task = None

        url = edge.rest.url.joinpath("channel", self.component.name, self.name)
        await self._rest_post(url, data)
        if update_cycle > 0:
            self._rest_update_task = edge.create_background_task(
                self._rest_write_cyclic(url, data, update_cycle, timeout)
            )

    async def _rest_write_cyclic(
        self, url: URL, data: dict[str, Any], update_cycle: float, timeout: float
    ) -> None:
        end = asyncio.get_running_loop().time() + timeout if timeout > 0 else None
        while True:
            await asyncio.sleep(update_cycle)
            if end is not None and asyncio.get_running_loop().time() >= end:
                return
            try:
                await self._rest_post(url, data)
            except OpenEMSError as err:
                _LOGGER.warning(
                    "Repeated REST write of channel %s/%s failed: %s",
                    self.component.name,
                    self.name,
                    err,
                )

    async def _rest_post(self, url: URL, data: dict[str, Any]) -> None:
        """Post a channel value to the REST API, raise OpenEMSError on failure."""
        connection = self.component.edge.backend.connection
        headers = {
            "Authorization": aiohttp.encode_basic_auth(
                connection.username, connection.password, encoding="latin1"
            )
        }

        async def _post(session: aiohttp.ClientSession) -> None:
            async with session.post(url, json=data, headers=headers) as resp:
                if resp.status != 200:
                    raise OpenEMSError(
                        f"REST write of channel {self.component.name}/{self.name} "
                        f"failed: HTTP {resp.status} {resp.reason}"
                    )

        try:
            if connection.session is not None:
                await _post(connection.session)
            else:
                async with aiohttp.ClientSession() as own_session:
                    await _post(own_session)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OpenEMSConnectionError(
                f"Cannot reach the REST API at {url.host}: {err}"
            ) from err


type DerivedSourceValue = str | float | None
type DerivedSourceValues = Mapping[str, DerivedSourceValue | list[DerivedSourceValue]]
type DerivedValueFn = Callable[[DerivedSourceValues], float | None]


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

    kind = ChannelKind.SENSOR

    def __init__(
        self,
        component: OpenEMSComponent,
        name: str,
        unit: str,
        sources: Mapping[str, str | list[str]],
        value_fn: DerivedValueFn,
    ) -> None:
        """Initialize the derived channel."""
        super().__init__(component, name, unit)
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


def _difference(values: DerivedSourceValues) -> float:
    return cast(float, values["max"]) - cast(float, values["min"])


def _spread(values: DerivedSourceValues) -> float:
    cells = cast(list[float], values["cells"])
    return max(cells) - min(cells)


# Channels calculated from other channels, key: component regexp
DERIVED_CHANNELS: dict[str, tuple[DerivedChannelDefinition, ...]] = {
    r"battery\d+": (
        DerivedChannelDefinition(
            id="VoltageDifference",
            sources={"max": "MaxCellVoltage", "min": "MinCellVoltage"},
            value_fn=_difference,
            unit="mV",
        ),
        DerivedChannelDefinition(
            id="Tower{tId}Module{mId}CellVoltageDifference",
            sources={"cells": "Tower{tId}Module{mId}Cell{cId}Voltage"},
            value_fn=_spread,
            unit="mV",
        ),
    ),
}


def derived_channel_definitions(
    component_name: str,
) -> tuple[DerivedChannelDefinition, ...]:
    """Return the derived channel definitions for the given component."""
    for component_regexp, definitions in DERIVED_CHANNELS.items():
        if re.fullmatch(component_regexp, component_name):
            return definitions
    return ()


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
        """Write a new value to the edge, raise ValueError if it is invalid."""
        await self.component.update_properties({self: self.to_backend_value(new_value)})


class OpenEMSEnumProperty(OpenEMSProperty):
    """Class representing a enum property of an OpenEMS component."""

    def __init__(
        self,
        component: OpenEMSComponent,
        channel_json: dict[str, Any],
        options: list[str],
    ) -> None:
        """Initialize the channel."""
        super().__init__(component, channel_json)
        # options as used by the backend
        self.property_options: list[str] = options
        self.option_keys = list(dict.fromkeys(to_snake_case(o) for o in options))

    def _channel_kind(self, channel_json: dict[str, Any]) -> ChannelKind:
        return ChannelKind.SELECT

    def to_backend_value(self, value: Any) -> str:
        """Convert the snake_case or original option to the original option."""
        for option in self.property_options:
            if value in (option, to_snake_case(option)):
                return option
        raise ValueError(f"Invalid option: {value}")

    @property
    def value(self) -> str | None:
        """Return the key of the current option."""
        if isinstance(self._current_value, str):
            return to_snake_case(self._current_value)

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

    def _channel_kind(self, channel_json: dict[str, Any]) -> ChannelKind:
        return ChannelKind.TIME

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

        self.multiplier_def = NumberExpression(str(self.multiplier), component)
        self.lower_limit_def = NumberExpression(str(self.lower_limit), component)
        self.upper_limit_def = NumberExpression(str(self.upper_limit), component)

        self.step: float = 1.0
        # fixed step size from the config, replaces the calculated step size
        self.fixed_step: float | None = None
        self.reference_channels: dict[str, str | float | None] = {}

    def _channel_kind(self, channel_json: dict[str, Any]) -> ChannelKind:
        return ChannelKind.NUMBER

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
        multiplier_value = self.multiplier_def.evaluate(self.reference_channels)
        # unknown, or 0 (eg no phases reported), which would make writes divide by 0
        multiplier = multiplier_value or 1.0
        lower_noscale = self.lower_limit_def.evaluate(self.reference_channels)
        upper_noscale = self.upper_limit_def.evaluate(self.reference_channels)

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

    def set_multiplier_def(self, multiplier_def: str) -> None:
        """Initialize the multiplier of the number channel."""
        self.multiplier_def = NumberExpression(multiplier_def, self.component)
        if not self.multiplier_def.linked_channels:
            # no external references. Calculate the result immediately
            self.multiplier = self.multiplier_def.constant()
        else:
            for ref in self.multiplier_def.linked_channels:
                self.reference_channels[ref] = None

    def set_step(self, step: float) -> None:
        """Initialize a fixed step size of the number channel."""
        self.fixed_step = step
        self.step = step

    def set_limit_def(self, limit_def: dict[str, str]) -> None:
        """Initialize the limits of the number channel."""
        self.lower_limit_def = NumberExpression(limit_def["lower"], self.component)
        lower_references = self.lower_limit_def.linked_channels
        if not lower_references:
            # no external references. Calculate the result immediately
            self.lower_limit = self.lower_limit_def.constant()
        else:
            for ref in lower_references:
                self.reference_channels[ref] = None

        self.upper_limit_def = NumberExpression(limit_def["upper"], self.component)
        upper_references = self.upper_limit_def.linked_channels
        if not upper_references:
            # no external references. Calculate the result immediately
            self.upper_limit = self.upper_limit_def.constant()
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

    def to_backend_value(self, value: Any) -> bool:
        """Convert booleans, 0/1 and strings like "on"/"off" to bool."""
        return to_bool(value)

    def _channel_kind(self, channel_json: dict[str, Any]) -> ChannelKind:
        return ChannelKind.SWITCH

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
        # implementation of the component, eg "Evcs.Keba.KeContact"
        self.factory_id: str | None = json_def.get("factoryId")
        self.has_primary_channels: bool = CONFIG.has_primary_channels(name)
        self.ref_values: dict[str, Any] = {}
        self.json_properties: dict[str, Any] = json_def["properties"]
        self._channels: list[OpenEMSDataHandler] = []
        self.channel_ids: list[str] = []

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the component.

        The hostname may contain spaces itself (multi edge setups: "<hostname> <edge id>").
        """
        return self.edge.hostname + " " + self.name

    def init_channels(self, channels: list[dict[str, Any]]) -> None:
        """Parse and initialize the components channels."""
        self.channel_ids = [c["id"] for c in channels]
        for channel_json in channels:
            options_backend: dict[str, int] | list[str] | None = channel_json.get(
                "options"
            )
            if channel_json["id"].startswith("_Property"):
                # scan type and convert to property
                match channel_json["type"]:
                    case "BOOLEAN":
                        self._channels.append(
                            OpenEMSBooleanProperty(
                                component=self, channel_json=channel_json
                            )
                        )
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
                            self._channels.append(enum_prop)
                        elif CONFIG.is_time_property(self.name, channel_json["id"]):
                            time_prop = OpenEMSTimeProperty(
                                component=self, channel_json=channel_json
                            )
                            self._channels.append(time_prop)
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
                                self._channels.append(number_prop)
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
                self._channels.append(
                    OpenEMSChannel(
                        component=self,
                        channel_json=channel_json,
                        options=channel_options,
                    )
                )
        self._channels.extend(
            self.create_derived_channels(derived_channel_definitions(self.name))
        )

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
        """Return all channels of the component, including derived channels."""
        return list(self._channels)

    @overload
    def channels_of(self, kind: ChannelKind) -> list[OpenEMSDataHandler]: ...

    @overload
    def channels_of[T: OpenEMSDataHandler](
        self, kind: ChannelKind, channel_type: type[T]
    ) -> list[T]: ...

    def channels_of(
        self,
        kind: ChannelKind,
        channel_type: type[OpenEMSDataHandler] = OpenEMSDataHandler,
    ) -> list[Any]:
        """Return the channels of the given kind, optionally narrowed to a channel class."""
        return [
            channel
            for channel in self._channels
            if channel.kind is kind and isinstance(channel, channel_type)
        ]

    @property
    def properties(self) -> list[OpenEMSProperty]:
        """Return all writable channels of the component."""
        return [
            channel
            for channel in self._channels
            if isinstance(channel, OpenEMSProperty)
        ]


class _OpenEMSEdgeVersion(OpenEMSDataHandler):
    """Software version of the edge, from the channel _meta/Version."""

    kind = ChannelKind.SENSOR

    def __init__(self, meta_component: OpenEMSComponent) -> None:
        super().__init__(meta_component, "Version")

    def handle_data_update(self, channel_name: str, value: BackendValue) -> None:
        # the last known version stays valid while the edge is not connected
        if isinstance(value, str) and value and value != self._current_value:
            self._current_value = value
            self._notify_listeners()

    @property
    def value(self) -> str | None:
        version: str | None = self._current_value
        return version

    def _subscribe(self) -> None:
        self.component.edge.register_channel({META_VERSION_CHANNEL}, self)

    def _unsubscribe(self) -> None:
        self.component.edge.unregister_channel(self)


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
        # connected and receiving channel data, see set_unavailable and currentData
        self._available: bool = False
        self.current_channel_data: dict[str, Any] = {}
        self._channel_subscription_updater = self.OpenEmsEdgeChannelSubscriptionUpdater(
            self
        )
        self._registered_handlers: dict[str, set[OpenEMSDataHandler]] = {}
        self.hostname: str = component_config["_host"]["Hostname"]
        self.is_fems: bool = is_fems(
            backend.connection.conn_type, self.hostname, component_config
        )
        if self.backend.multi_edge:
            self.hostname += " " + self.id
        # last update state with versions, the range of the release notes
        self._update_state: SystemUpdateState | None = None
        # changelog of FEMS releases, by the latest version it was read for
        self._changelog: tuple[str, list[ChangelogRelease]] | None = None

        self.components: dict[str, OpenEMSComponent] = self._prepare_entities(
            component_config
        )
        self.rest: RestDetails | None = self._prepare_rest(component_config)
        # eg cyclic REST writes, cancelled when the edge stops
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._version: _OpenEMSEdgeVersion | None = (
            _OpenEMSEdgeVersion(self.components["_meta"])
            if "_meta" in self.components
            else None
        )

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

    @property
    def available(self) -> bool:
        """Return True while the edge is connected and sends channel data."""
        return self._available

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for handler in {h for hs in self._registered_handlers.values() for h in hs}:
            handler._notify_listeners()  # noqa: SLF001

    def set_unavailable(self) -> None:
        """Clear all channel values and mark the edge as unavailable.

        Called by the connection logic on connection loss. The edge is available
        again with the first channel data after the reconnect.
        """
        for channel_name in self.current_channel_data:
            for handler in self._registered_handlers.get(channel_name, ()):
                handler.handle_data_update(channel_name, None)
        self.current_channel_data = {}
        self._channel_subscription_updater.clear()
        self._set_available(False)

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
        for task in list(self._background_tasks):
            await cancel_task(task)

    def create_background_task(
        self, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Run a coroutine in a task, which is cancelled when the edge stops."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    @property
    def rest_writable(self) -> bool:
        """Return True if the edge has a REST app with write access."""
        return self.rest is not None and self.rest.mode == "ReadWrite"

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
        self._set_available(True)

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
    def version(self) -> str | None:
        """Return the software version of the edge, None until it is known.

        The version is only read while a version listener is registered.
        """
        return self._version.value if self._version else None

    def add_version_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Add a listener called when the version changes, return a function to remove it.

        The listener is also called on availability changes of the edge. The
        version channel is subscribed at the edge while it has listeners.
        """
        if self._version is None:
            return lambda: None
        return self._version.add_listener(listener)

    @property
    def id(self) -> str:
        """Return the edge ID."""
        return self._id

    @property
    def registered_channels(self) -> dict[str, set[OpenEMSDataHandler]]:
        "Return the list of all subscribed channels."
        return self._registered_handlers

    async def get_system_update_state(self) -> SystemUpdateState:
        """Read the state of the system update."""
        system_update_state_call = wrap_jsonrpc("getSystemUpdateState")
        edge_call = wrap_jsonrpc(
            "componentJsonApi", componentId="_host", payload=system_update_state_call
        )
        result = await backend_call(
            self.backend.connection.rpc_server.edgeRpc(
                edgeId=self._id, payload=edge_call
            )
        )
        state = parse_system_update_state(result["payload"]["result"])
        if state.installed_version is not None:
            self._update_state = state
        return state

    @property
    def manufacturer(self) -> str | None:
        """Return the manufacturer of the edge, if known."""
        return FENECON if self.is_fems else None

    @property
    def release_url(self) -> str:
        """Return the page with the changes of the software releases."""
        connection = self.backend.connection
        return release_url(connection.conn_type, connection.conn_url.host, self.is_fems)

    @property
    def has_release_notes(self) -> bool:
        """Return True if release notes are available, see get_release_notes."""
        return self.is_fems

    async def get_release_notes(self, language: str) -> str | None:
        """Return the changes from the installed to the latest version as Markdown.

        The versions are taken from the last update state, see
        get_system_update_state. Only FEMS publish release notes, each release is
        given in the requested language (eg "de"), otherwise English or German.
        Return None if there are none or they cannot be read.
        """
        if not self.is_fems:
            return None
        state = self._update_state
        if state is None:
            try:
                state = await self.get_system_update_state()
            except OpenEMSError:
                return None
        if state.installed_version is None or state.latest_version is None:
            return None
        if self._changelog is None or self._changelog[0] != state.latest_version:
            try:
                releases = await self._read_changelog()
            except OpenEMSError as err:
                _LOGGER.debug("Cannot read the FEMS changelog: %s", err)
                return None
            self._changelog = (state.latest_version, releases)
        return release_notes(
            releases_between(
                self._changelog[1], state.installed_version, state.latest_version
            ),
            language,
        )

    async def _read_changelog(self) -> list[ChangelogRelease]:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

        async def _get(session: aiohttp.ClientSession) -> list[ChangelogRelease]:
            async with session.get(FEMS_CHANGELOG_URL, timeout=timeout) as resp:
                resp.raise_for_status()
                return parse_changelog(await resp.json(content_type=None))

        session = self.backend.connection.session
        try:
            if session is not None:
                return await _get(session)
            async with aiohttp.ClientSession() as own_session:
                return await _get(own_session)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise OpenEMSConnectionError(
                f"Cannot read the FEMS changelog: {err}"
            ) from err

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

    async def install_system_update(
        self,
        on_progress: Callable[[SystemUpdateState], None] | None = None,
        *,
        timeout: float = 900,
        poll_interval: float = 10,
    ) -> SystemUpdateState:
        """Install the available system update and wait until it is finished.

        The state is polled every poll_interval seconds and passed to on_progress.
        It is UNKNOWN while the edge does not respond, eg while it restarts.
        Return the state once the edge reports to be up to date. Raise the error of
        the update request if the edge rejects it, OpenEMSTimeoutError if the update
        did not finish within timeout seconds.
        """
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        update_task = asyncio.create_task(self.execute_system_update())
        try:
            # give the edge some time to start the update before checking progress
            await asyncio.sleep(min(0.5, poll_interval))
            while loop.time() < end:
                try:
                    state = await self.get_system_update_state()
                except OpenEMSError:
                    state = SystemUpdateState(status=UpdateStatus.UNKNOWN)
                if on_progress is not None:
                    on_progress(state)
                # connection losses are expected, as the edge restarts during the update
                if (
                    update_task.done()
                    and (error := update_task.exception()) is not None
                    and not isinstance(error, OpenEMSConnectionError)
                ):
                    raise error
                if state.status is UpdateStatus.UP_TO_DATE:
                    return state
                await asyncio.sleep(poll_interval)
        finally:
            if not update_task.done():
                update_task.cancel()
            elif not update_task.cancelled():
                # retrieve the result, a connection loss of the restarting edge is expected
                update_task.exception()
        raise OpenEMSTimeoutError(
            f"System update of edge {self.hostname} did not finish within "
            f"{timeout} seconds"
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

    def start(self, auth_failed_callback: Callable[[], None] | None = None) -> None:
        """Start the reconnect logic and the background tasks of the edge.

        auth_failed_callback is called when the backend rejects the login of a
        reconnect, the reconnects stop then.
        """
        self.connection.enable_reconnect(
            self.the_edge.set_unavailable, auth_failed_callback
        )
        self.the_edge.start()

    async def stop(self) -> None:
        """Close the connection and wait until all background tasks have finished."""
        await self.connection.stop()
        await self.the_edge.stop()
