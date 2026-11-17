"""Helper methods using openems classes, eg during channel creation."""

from enum import IntFlag
import re
from typing import TYPE_CHECKING, cast

from jinja2 import Template

from .const import SLASH_ESC

if TYPE_CHECKING:
    from .openems import DerivedChannelDefinition, OpenEMSChannel, OpenEMSComponent


class OpenEMSAccessMode(IntFlag):
    """Access modes of openems channels."""

    NONE = 0
    READ = 1
    WRITE = 2


def prepare_ref_value(
    expr: str, component: OpenEMSComponent
) -> tuple[Template, list[str]]:
    """Parse a template string into a template and channels contained."""
    linked_channels = []

    def calc_component_reference(matchobj) -> str:
        nonlocal linked_channels
        match = matchobj.group()[2:-2]
        if "/" in match:
            comp_ref, channel = match.split("/")
            if comp_ref[0] == "$":
                # if the reference starts with $, treat the component like a variable,
                # to be looked up in the component properties
                # replace all linked channels with their values
                comp_ref = component.json_properties[comp_ref[1:]]
        else:
            comp_ref, channel = component.name, match

        # prepare value containers of required channels
        linked_channel = comp_ref + SLASH_ESC + channel
        if linked_channel not in linked_channels:
            linked_channels.append(linked_channel)
        return linked_channel

    value_expr = "{{" + re.sub(r"{{(.*?)}}", calc_component_reference, expr) + "}}"
    return Template(value_expr), linked_channels


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
