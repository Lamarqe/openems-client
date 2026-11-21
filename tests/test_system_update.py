"""Tests for the system update of an edge."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from yarl import URL

from openems_client.exceptions import (
    OpenEMSConnectionError,
    OpenEMSError,
    OpenEMSTimeoutError,
)
from openems_client.openems import OpenEMSEdge
from openems_client.system_update import (
    SystemUpdateState,
    UpdateStatus,
    parse_system_update_state,
)

UP_TO_DATE = SystemUpdateState(
    status=UpdateStatus.UP_TO_DATE,
    installed_version="2026.8.1",
    latest_version="2026.8.1",
)
AVAILABLE = SystemUpdateState(
    status=UpdateStatus.AVAILABLE,
    installed_version="2026.7.2",
    latest_version="2026.8.1",
)
RUNNING = SystemUpdateState(status=UpdateStatus.RUNNING, percent_completed=40)
UNKNOWN = SystemUpdateState(status=UpdateStatus.UNKNOWN)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        pytest.param({"updated": {"version": "2026.8.1"}}, UP_TO_DATE, id="up_to_date"),
        pytest.param(
            {"available": {"currentVersion": "2026.7.2", "latestVersion": "2026.8.1"}},
            AVAILABLE,
            id="available",
        ),
        pytest.param(
            {"running": {"percentCompleted": 40, "logs": []}}, RUNNING, id="running"
        ),
        pytest.param({"somethingNew": {}}, UNKNOWN, id="unknown_status"),
        pytest.param({}, UNKNOWN, id="empty"),
    ],
)
def test_parse_system_update_state(result: dict, expected: SystemUpdateState) -> None:
    """The raw getSystemUpdateState result is parsed into a typed state."""
    assert parse_system_update_state(result) == expected


def _make_edge() -> OpenEMSEdge:
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/")
    return OpenEMSEdge(backend, "0", {"_host": {"Hostname": "fems1"}})


async def test_get_system_update_state() -> None:
    """The state is read via the host component and returned typed."""
    edge = _make_edge()
    edge.backend.connection.rpc_server.edgeRpc = AsyncMock(
        return_value={"payload": {"result": {"updated": {"version": "2026.8.1"}}}}
    )

    assert await edge.get_system_update_state() == UP_TO_DATE

    payload = edge.backend.connection.rpc_server.edgeRpc.call_args.kwargs["payload"]
    assert payload["params"]["componentId"] == "_host"
    assert payload["params"]["payload"]["method"] == "getSystemUpdateState"


async def test_install_system_update() -> None:
    """The update is installed and its progress reported until it is finished."""
    edge = _make_edge()
    # the edge restarts during the update, so the update request loses its connection
    edge.execute_system_update = AsyncMock(
        side_effect=OpenEMSConnectionError("connection lost")
    )
    edge.get_system_update_state = AsyncMock(
        side_effect=[AVAILABLE, RUNNING, OpenEMSConnectionError("restart"), UP_TO_DATE]
    )
    progress: list[SystemUpdateState] = []

    state = await edge.install_system_update(progress.append, poll_interval=0.001)

    assert state == UP_TO_DATE
    assert progress == [AVAILABLE, RUNNING, UNKNOWN, UP_TO_DATE]
    edge.execute_system_update.assert_awaited_once()


async def test_install_system_update_rejected() -> None:
    """A rejected update request is raised."""
    edge = _make_edge()
    edge.execute_system_update = AsyncMock(side_effect=OpenEMSError("rejected"))
    edge.get_system_update_state = AsyncMock(return_value=AVAILABLE)

    with pytest.raises(OpenEMSError, match="rejected"):
        await edge.install_system_update(poll_interval=0.001)


async def test_install_system_update_timeout() -> None:
    """An update which does not finish in time raises and stops waiting for it."""
    edge = _make_edge()
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()

    async def _never_finishing_request() -> None:
        request_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            request_cancelled.set()
            raise

    edge.execute_system_update = _never_finishing_request
    edge.get_system_update_state = AsyncMock(return_value=RUNNING)

    with pytest.raises(OpenEMSTimeoutError):
        await edge.install_system_update(timeout=0.05, poll_interval=0.01)

    assert request_started.is_set()
    await asyncio.sleep(0)
    assert request_cancelled.is_set()


async def test_execute_system_update_request() -> None:
    """The update is triggered via the host component, without a timeout."""
    edge = _make_edge()
    edge.backend.connection.rpc_server.edgeRpc = AsyncMock(return_value={})

    await edge.execute_system_update()

    payload = edge.backend.connection.rpc_server.edgeRpc.call_args.kwargs["payload"]
    assert payload["params"]["componentId"] == "_host"
    assert payload["params"]["payload"]["method"] == "executeSystemUpdate"
