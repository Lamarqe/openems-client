"""State of the system update of an edge."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class UpdateStatus(StrEnum):
    """Status of the system update."""

    UP_TO_DATE = "updated"
    AVAILABLE = "available"
    RUNNING = "running"
    # unknown status, or the edge does not respond, eg while it restarts
    UNKNOWN = "unknown"


@dataclass(frozen=True, kw_only=True)
class SystemUpdateState:
    """State of the system update, as reported by the edge."""

    status: UpdateStatus
    installed_version: str | None = None
    # equals installed_version when up to date
    latest_version: str | None = None
    # only while the update is running
    percent_completed: int | None = None


def parse_system_update_state(result: dict[str, Any]) -> SystemUpdateState:
    """Parse the result of getSystemUpdateState.

    The result has one key, the status: {"updated": {"version": ...}},
    {"available": {"currentVersion": ..., "latestVersion": ...}} or
    {"running": {"percentCompleted": ...}}.
    """
    if not result:
        return SystemUpdateState(status=UpdateStatus.UNKNOWN)
    status, details = next(iter(result.items()))
    match status:
        case UpdateStatus.UP_TO_DATE:
            return SystemUpdateState(
                status=UpdateStatus.UP_TO_DATE,
                installed_version=details["version"],
                latest_version=details["version"],
            )
        case UpdateStatus.AVAILABLE:
            return SystemUpdateState(
                status=UpdateStatus.AVAILABLE,
                installed_version=details["currentVersion"],
                latest_version=details["latestVersion"],
            )
        case UpdateStatus.RUNNING:
            return SystemUpdateState(
                status=UpdateStatus.RUNNING,
                percent_completed=details.get("percentCompleted"),
            )
        case _:
            return SystemUpdateState(status=UpdateStatus.UNKNOWN)
