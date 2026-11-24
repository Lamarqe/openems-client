"""Classification of OpenEMS channels, independent of any consumer."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re

from .helpers import to_snake_case

PROPERTY_PREFIX = "_Property"

_CAMEL_CASE_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# upper case letters with digits (L1, PT1), upper case runs, words, numbers
_WORD_PATTERN = re.compile(r"[A-Z]+\d+(?![a-z])|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


class ChannelKind(StrEnum):
    """How a channel is represented: read-only value or writable setting."""

    SENSOR = "sensor"
    ENUM_SENSOR = "enum_sensor"
    # read-only text, eg serial numbers and versions
    TEXT_SENSOR = "text_sensor"
    # write-only, written via the REST API, eg setpoints and commands
    WRITE_ONLY = "write_only"
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
    # backend values are divided by it to match the normalised unit
    divisor: int = 1


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
    "°dC": ("°C", Quantity.TEMPERATURE, False),
}

# units whose values are scaled to the normalised unit, eg decidegree Celsius
_DIVISORS: dict[str, int] = {"°dC": 10}


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
    return UnitInfo(
        unit=normalised,
        quantity=quantity,
        cumulative=cumulative,
        divisor=_DIVISORS.get(unit, 1),
    )


def channel_key(name: str) -> str:
    """Return the snake_case key of a channel name, without the property prefix.

    Eg "_PropertyChargeMode" -> "charge_mode", "EssSoc" -> "ess_soc".
    """
    return to_snake_case(
        _CAMEL_CASE_PATTERN.sub("_", name.removeprefix(PROPERTY_PREFIX))
    )


def display_name(name: str, acronyms: Mapping[str, str]) -> str:
    """Return a readable English name of a channel name, without the property prefix.

    Eg "_PropertyMaxDcChargePower" -> "Max DC charge power",
    "Tower0Module3Cell002Voltage" -> "Tower 0 module 3 cell 2 voltage". Words found in
    acronyms (by lower case word) get its spelling, numbers keep their value.
    """
    words: list[str] = []
    for word in _WORD_PATTERN.findall(name.removeprefix(PROPERTY_PREFIX)):
        if (spelling := acronyms.get(word.lower())) is not None:
            words.append(spelling)
        elif word.isdigit():
            words.append(str(int(word)))
        elif word.isupper() or word[-1].isdigit():
            # upper case words and tokens like L1, V2, PT1
            words.append(word)
        else:
            words.append(word.lower())
    text = " ".join(words)
    return text[:1].upper() + text[1:]


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
