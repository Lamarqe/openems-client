"""Classification of OpenEMS channels, independent of any consumer."""

from dataclasses import dataclass
from enum import StrEnum
import re

from .helpers import to_snake_case

PROPERTY_PREFIX = "_Property"

_CAMEL_CASE_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


class ChannelKind(StrEnum):
    """How a channel is represented: read-only value or writable setting."""

    SENSOR = "sensor"
    ENUM_SENSOR = "enum_sensor"
    BINARY_SENSOR = "binary_sensor"
    NUMBER = "number"
    SELECT = "select"
    SWITCH = "switch"
    TIME = "time"


class Quantity(StrEnum):
    """Physical quantity of a channel value."""

    APPARENT_POWER = "apparent_power"
    CURRENT = "current"
    DURATION = "duration"
    ENERGY = "energy"
    FREQUENCY = "frequency"
    PERCENTAGE = "percentage"
    POWER = "power"
    PRESSURE = "pressure"
    REACTIVE_POWER = "reactive_power"
    STATE_OF_CHARGE = "state_of_charge"
    TEMPERATURE = "temperature"
    VOLTAGE = "voltage"


class Severity(StrEnum):
    """Level of a state channel, which signals a problem when active."""

    INFO = "info"
    WARNING = "warning"
    FAULT = "fault"


@dataclass(frozen=True, kw_only=True)
class UnitInfo:
    """Normalised unit of a channel."""

    unit: str | None
    quantity: Quantity | None
    # the value is a counter, which only increases
    cumulative: bool


# reference: openems/io.openems.common/src/io/openems/common/channel/Unit.java
# OpenEMS unit: (normalised unit, quantity, cumulative)
_UNITS: dict[str, tuple[str, Quantity, bool]] = {
    "Wh": ("Wh", Quantity.ENERGY, False),
    "kWh": ("kWh", Quantity.ENERGY, False),
    "Wh_Σ": ("Wh", Quantity.ENERGY, True),
    "W": ("W", Quantity.POWER, False),
    "mW": ("mW", Quantity.POWER, False),
    "kW": ("kW", Quantity.POWER, False),
    "A": ("A", Quantity.CURRENT, False),
    "mA": ("mA", Quantity.CURRENT, False),
    "Hz": ("Hz", Quantity.FREQUENCY, False),
    "mHz": ("mHz", Quantity.FREQUENCY, False),
    "h": ("h", Quantity.DURATION, False),
    "min": ("min", Quantity.DURATION, False),
    "s": ("s", Quantity.DURATION, False),
    "sec": ("s", Quantity.DURATION, False),
    "ms": ("ms", Quantity.DURATION, False),
    "sec_Σ": ("s", Quantity.DURATION, True),
    "%": ("%", Quantity.PERCENTAGE, False),
    "V": ("V", Quantity.VOLTAGE, False),
    "mV": ("mV", Quantity.VOLTAGE, False),
    "bar": ("bar", Quantity.PRESSURE, False),
    "mbar": ("mbar", Quantity.PRESSURE, False),
    "var": ("var", Quantity.REACTIVE_POWER, False),
    "VA": ("VA", Quantity.APPARENT_POWER, False),
    "C": ("°C", Quantity.TEMPERATURE, False),
    "°C": ("°C", Quantity.TEMPERATURE, False),
}


def unit_info(unit: str | None, key: str) -> UnitInfo:
    """Normalise the OpenEMS unit of the channel with the given key."""
    if not unit:
        return UnitInfo(unit=None, quantity=None, cumulative=False)
    if unit not in _UNITS:
        return UnitInfo(unit=unit, quantity=None, cumulative=False)
    normalised, quantity, cumulative = _UNITS[unit]
    # percentages are a state of charge only for SoC channels, eg efficiencies are not
    if quantity is Quantity.PERCENTAGE and (key == "soc" or key.endswith("_soc")):
        quantity = Quantity.STATE_OF_CHARGE
    return UnitInfo(unit=normalised, quantity=quantity, cumulative=cumulative)


def channel_key(name: str) -> str:
    """Return the snake_case key of a channel name, without the property prefix.

    Eg "_PropertyChargeMode" -> "charge_mode", "EssSoc" -> "ess_soc".
    """
    return to_snake_case(
        _CAMEL_CASE_PATTERN.sub("_", name.removeprefix(PROPERTY_PREFIX))
    )


def severity(level: str | None) -> Severity | None:
    """Return the severity of an OpenEMS channel level."""
    match level:
        case "INFO":
            return Severity.INFO
        case "WARNING":
            return Severity.WARNING
        case "FAULT":
            return Severity.FAULT
        case _:
            return None
