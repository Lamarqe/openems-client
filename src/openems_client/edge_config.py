"""Comparison of edge configurations, as read by OpenEMSConfigReader."""

from dataclasses import dataclass, field
import json
from typing import Any

# channel attributes, which determine how a channel is represented
_CHANNEL_ATTRIBUTES = ("type", "unit", "accessMode", "category", "level")


@dataclass(frozen=True, kw_only=True)
class EdgeConfigChange:
    """Structural difference between two edge configurations.

    Property values are not part of it: they are provided by the _Property channels.
    """

    added: frozenset[str] = field(default_factory=frozenset)
    removed: frozenset[str] = field(default_factory=frozenset)
    # factoryId or channels changed
    changed: frozenset[str] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        """Return True if components were added, removed or changed."""
        return bool(self.added or self.removed or self.changed)


def _channel_signature(component: dict[str, Any]) -> frozenset[str]:
    return frozenset(
        json.dumps(
            {
                "id": channel["id"],
                "options": channel.get("options"),
                **{attr: channel.get(attr) for attr in _CHANNEL_ATTRIBUTES},
            },
            sort_keys=True,
        )
        for channel in component.get("channels", [])
    )


def edge_config_change(old: dict[str, Any], new: dict[str, Any]) -> EdgeConfigChange:
    """Return the structural difference of two edge configurations."""
    return EdgeConfigChange(
        added=frozenset(new.keys() - old.keys()),
        removed=frozenset(old.keys() - new.keys()),
        changed=frozenset(
            name
            for name in old.keys() & new.keys()
            if old[name].get("factoryId") != new[name].get("factoryId")
            or _channel_signature(old[name]) != _channel_signature(new[name])
        ),
    )
