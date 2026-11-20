"""Load additional config options from json files."""

import json
from pathlib import Path
import re
from typing import Any

ENCODING = "utf-8"


class OpenEMSConfig:
    """Load additional config options from json files."""

    def __init__(self) -> None:
        """Initialize and read json files."""
        path = Path(__file__).parent / "config"
        with (path / "enum_options.json").open(encoding=ENCODING) as enum_file:
            self.enum_options: list[dict[str, Any]] = json.load(enum_file)
        with (path / "time_options.json").open(encoding=ENCODING) as time_file:
            self.time_options: list[dict[str, Any]] = json.load(time_file)
        with (path / "number_properties.json").open(encoding=ENCODING) as number_file:
            self.number_properties: list[dict[str, Any]] = json.load(number_file)
        with (path / "component_update_groups.json").open(
            encoding=ENCODING
        ) as groups_file:
            self.update_groups: list[dict[str, Any]] = json.load(groups_file)

    def _get_config_property(
        self,
        config: list[dict[str, Any]],
        property: str,
        component_name: str,
        channel_name: str,
    ) -> Any:
        """Return dict property for a given component/channel."""
        for component_conf in config:
            comp_regex = component_conf["component_regexp"]
            if re.fullmatch(comp_regex, component_name):
                for channel in component_conf["channels"]:
                    if channel["id"] == channel_name:
                        return channel.get(property)
        return None

    def get_enum_options(
        self, component_name: str, channel_name: str
    ) -> list[str] | None:
        """Return option string list for a given component/channel."""
        options: list[str] | None = self._get_config_property(
            self.enum_options, "options", component_name, channel_name
        )
        return options

    def is_time_property(self, component_name: str, channel_name: str) -> bool:
        """Return True if given component/channel is marked as time."""
        return bool(
            self._get_config_property(
                self.time_options, "is_time", component_name, channel_name
            )
        )

    def get_number_limit(
        self, component_name: str, channel_name: str
    ) -> dict[str, str] | None:
        """Return limit definition for a given component/channel."""
        limit: dict[str, str] | None = self._get_config_property(
            self.number_properties, "limit", component_name, channel_name
        )
        return limit

    def get_number_multiplier(
        self, component_name: str, channel_name: str
    ) -> str | None:
        """Return the multiplier template for a given component/channel."""
        multiplier: str | None = self._get_config_property(
            self.number_properties, "multiplier", component_name, channel_name
        )
        return multiplier

    def get_number_step(self, component_name: str, channel_name: str) -> float | None:
        """Return the fixed step size for a given component/channel."""
        step: float | None = self._get_config_property(
            self.number_properties, "step", component_name, channel_name
        )
        return step

    def update_group_members(
        self, comp_name: str, chan_name: str
    ) -> tuple[list[str], Any]:
        """Return list of all update group members and the condition value."""
        for entry in self.update_groups:
            if re.fullmatch(entry["component_regexp"], comp_name):
                for rule in entry["rules"]:
                    if rule["channel"] == chan_name:
                        requires: list[str] = rule["requires"]
                        return requires, rule.get("when")

        return [], None
