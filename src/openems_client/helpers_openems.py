"""Helper methods using openems classes, eg during channel creation."""

from collections.abc import Mapping
from enum import IntFlag
import re
from typing import TYPE_CHECKING, cast

from jinja2 import StrictUndefined, UndefinedError
from jinja2.sandbox import ImmutableSandboxedEnvironment

from .const import SLASH_ESC

if TYPE_CHECKING:
    from .openems import (
        BackendValue,
        DerivedChannelDefinition,
        OpenEMSChannel,
        OpenEMSComponent,
    )


class OpenEMSAccessMode(IntFlag):
    """Access modes of openems channels."""

    NONE = 0
    READ = 1
    WRITE = 2


_SANDBOX = ImmutableSandboxedEnvironment(undefined=StrictUndefined)
_REFERENCE = re.compile(r"{{(.*?)}}")


def _linked_channel(reference: str, component: OpenEMSComponent) -> str:
    """Return the key of a referenced channel, eg "$ess.id/MaxApparentPower"."""
    if "/" in reference:
        comp_ref, channel = reference.split("/")
        if comp_ref[0] == "$":
            # the component is the value of a property, eg "ess.id"
            comp_ref = component.json_properties[comp_ref[1:]]
    else:
        comp_ref, channel = component.name, reference
    return comp_ref + SLASH_ESC + channel


class NumberExpression:
    """Number expression of the config, eg a limit referencing other channels.

    The references ({{<component>/<channel>}}) are replaced with generated
    placeholders, so values from the edge never become part of the expression,
    which is evaluated in a sandbox.
    """

    def __init__(self, expr: str, component: OpenEMSComponent) -> None:
        """Compile the expression, raise TemplateSyntaxError if it is invalid."""
        # placeholder -> key of the referenced channel
        self._references: dict[str, str] = {}

        def _placeholder(match: re.Match[str]) -> str:
            linked_channel = _linked_channel(match.group(1), component)
            for name, reference in self._references.items():
                if reference == linked_channel:
                    return name
            name = f"ref{len(self._references)}"
            self._references[name] = linked_channel
            return name

        self.expr = expr
        self._expression = _SANDBOX.compile_expression(
            _REFERENCE.sub(_placeholder, expr)
        )

    @property
    def linked_channels(self) -> list[str]:
        """Return the keys of the referenced channels."""
        return list(self._references.values())

    def evaluate(self, values: Mapping[str, BackendValue]) -> float | None:
        """Return the result for the values of the referenced channels.

        Return None if a value is missing or not a number, or on division by zero.
        """
        try:
            result = self._expression(
                **{
                    name: values.get(reference)
                    for name, reference in self._references.items()
                }
            )
            return None if result is None else float(result)
        except UndefinedError, TypeError, ValueError, ZeroDivisionError:
            return None

    def constant(self) -> float:
        """Return the value of an expression without references."""
        if (value := self.evaluate({})) is None:
            raise ValueError(f"Invalid number expression: {self.expr}")
        return value


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    """Convert a channel id pattern with {name} placeholders into a regex."""
    parts = _PLACEHOLDER.split(pattern)
    # split() alternates literal text and placeholder names
    regex = "".join(
        re.escape(part) if i % 2 == 0 else f"(?P<{part}>[^{{}}]+)"
        for i, part in enumerate(parts)
    )
    return re.compile(regex)


def expand_derived_definition(
    definition: DerivedChannelDefinition, channel_ids: list[str]
) -> list[tuple[str, dict[str, str | list[str]]]]:
    """Expand a derived channel definition against the channel ids of a component.

    Placeholders in the definition id select one derived channel per value
    combination. Placeholders that only occur in a source pattern turn that
    source into a list of all matching channel ids.
    Returns (derived channel id, {source name: channel id(s)}) tuples.
    """
    key_names = _PLACEHOLDER.findall(definition.id)
    if not key_names and not any(
        _PLACEHOLDER.search(pattern) for pattern in definition.sources.values()
    ):
        return [(definition.id, dict(definition.sources))]

    expanded: dict[tuple[str, ...], dict[str, str | list[str]]] = {}
    for source_name, pattern in definition.sources.items():
        regex = _pattern_regex(pattern)
        is_list = any(name not in key_names for name in _PLACEHOLDER.findall(pattern))
        for channel_id in channel_ids:
            if not (match := regex.fullmatch(channel_id)):
                continue
            key = tuple(match.group(name) for name in key_names)
            sources = expanded.setdefault(key, {})
            if is_list:
                cast(list[str], sources.setdefault(source_name, [])).append(channel_id)
            else:
                sources[source_name] = channel_id

    return [
        (definition.id.format(**dict(zip(key_names, key, strict=True))), sources)
        for key, sources in expanded.items()
        # all sources must exist, otherwise the value can never be calculated
        if sources.keys() == definition.sources.keys()
    ]


def access_mode(channel: OpenEMSChannel) -> OpenEMSAccessMode:
    """Derive access mode for given channel."""
    match channel.orig_json.get("accessMode", ""):
        case "RO":
            return OpenEMSAccessMode.READ
        case "RW":
            return OpenEMSAccessMode.READ | OpenEMSAccessMode.WRITE
        case "WO":
            return OpenEMSAccessMode.WRITE
        case _:
            return OpenEMSAccessMode.NONE
