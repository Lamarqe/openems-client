"""Tests for the FEMS detection, release URLs and release notes."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from yarl import URL

from openems_client.const import (
    CONN_TYPE_CUSTOM_URL,
    CONN_TYPE_DIRECT_EDGE,
    CONN_TYPE_LOCAL_FEMS,
    CONN_TYPE_LOCAL_OPENEMS,
    CONN_TYPE_WEB_FENECON,
)
from openems_client.exceptions import OpenEMSConnectionError
from openems_client.fems import (
    FEMS_CHANGELOG_URL,
    ChangelogEntry,
    is_fems,
    parse_changelog,
    release_notes,
    release_url,
    releases_between,
)
from openems_client.openems import OpenEMSEdge
from openems_client.system_update import SystemUpdateState, UpdateStatus

FENECON_BACKEND = {
    "ctrlBackend0": {
        "factoryId": "Controller.Api.Backend",
        "properties": {"uri": "wss://www1.fenecon.de:443/openems-backend2"},
    }
}
OTHER_BACKEND = {
    "ctrlBackend0": {
        "factoryId": "Controller.Api.Backend",
        "properties": {"uri": "wss://backend.example.com/openems"},
    }
}

CHANGELOG: list[dict[str, Any]] = [
    {"version": "FENECON App (16.11.2026)", "entries": {"en": ["App only"]}},
    {
        "version": "2026.11.1",
        "entries": {
            "de": ["Neues Design"],
            "en": [
                "New design, see <a class='ion-link' target=\"_blank\" "
                'href="https://docs.fenecon.de/x">documentation</a>',
                {"roleIsAtLeast": "ADMIN", "text": "Internal fix"},
                "Home &amp; Commercial: dynamic feed-in limit",
            ],
        },
    },
    {"version": "2026.10.2", "entries": {"de": ["Fehlerbehebung"], "en": ["Bug fix"]}},
    {
        "version": "2026.9.4",
        "entries": {"en": [{"roleIsAtLeast": "INSTALLER", "text": "Installer only"}]},
    },
    {"version": "2025.12.1", "entries": {"de": ["Nur deutsch"]}},
    {"version": "2025.11.3", "entries": {"de": ["Bereits installiert"]}},
]


@pytest.mark.parametrize(
    ("connection_type", "hostname", "components", "expected"),
    [
        pytest.param(CONN_TYPE_LOCAL_FEMS, "edge", {}, True, id="local_fems"),
        pytest.param(CONN_TYPE_WEB_FENECON, "edge", {}, True, id="fenecon_portal"),
        pytest.param(CONN_TYPE_DIRECT_EDGE, "fems57988", {}, True, id="fems_hostname"),
        pytest.param(
            CONN_TYPE_DIRECT_EDGE, "edge", FENECON_BACKEND, True, id="fenecon_backend"
        ),
        pytest.param(
            CONN_TYPE_CUSTOM_URL, "edge", OTHER_BACKEND, False, id="other_backend"
        ),
        pytest.param(CONN_TYPE_LOCAL_OPENEMS, "openems", {}, False, id="openems"),
        pytest.param(CONN_TYPE_DIRECT_EDGE, "femsbox", {}, False, id="hostname_prefix"),
    ],
)
def test_is_fems(
    connection_type: str, hostname: str, components: dict, expected: bool
) -> None:
    """FEMS are detected by connection type, hostname and backend."""
    assert is_fems(connection_type, hostname, components) is expected


@pytest.mark.parametrize(
    ("connection_type", "host", "fems", "expected"),
    [
        pytest.param(
            CONN_TYPE_WEB_FENECON,
            "portal.fenecon.de",
            True,
            "https://portal.fenecon.de/_/changelog",
            id="fenecon_portal",
        ),
        pytest.param(
            CONN_TYPE_LOCAL_FEMS,
            "192.0.2.1",
            True,
            "http://192.0.2.1/_/changelog",
            id="local_fems",
        ),
        pytest.param(
            CONN_TYPE_DIRECT_EDGE,
            "192.0.2.1",
            True,
            "http://192.0.2.1/_/changelog",
            id="direct_edge_fems",
        ),
        pytest.param(
            CONN_TYPE_DIRECT_EDGE,
            "192.0.2.1",
            False,
            "https://github.com/OpenEMS/openems/releases",
            id="openems",
        ),
    ],
)
def test_release_url(
    connection_type: str, host: str, fems: bool, expected: str
) -> None:
    """FEMS link the changelog page of the UI, OpenEMS the GitHub releases."""
    assert release_url(connection_type, host, fems) == expected


def test_parse_changelog() -> None:
    """System releases are parsed, entries of the app are skipped."""
    releases = parse_changelog(CHANGELOG)

    assert [release.version for release in releases] == [
        "2026.11.1",
        "2026.10.2",
        "2026.9.4",
        "2025.12.1",
        "2025.11.3",
    ]
    assert releases[0].entries["en"][1] == ChangelogEntry(
        text="Internal fix", min_role="ADMIN"
    )
    assert parse_changelog({"unexpected": "format"}) == []


def test_releases_between() -> None:
    """Releases after the installed up to the latest version, compared numerically."""
    releases = parse_changelog(CHANGELOG)

    selected = releases_between(releases, "2025.11.3", "2026.10.2")

    assert [release.version for release in selected] == [
        "2026.10.2",
        "2026.9.4",
        "2025.12.1",
    ]
    assert releases_between(releases, "unknown", "2026.10.2") == []


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        pytest.param(
            "en",
            "## 2026.11.1\n\n"
            "- New design, see [documentation](https://docs.fenecon.de/x)\n"
            "- Home & Commercial: dynamic feed-in limit\n\n"
            "## 2026.10.2\n\n- Bug fix\n\n"
            "## 2025.12.1\n\n- Nur deutsch",
            id="english",
        ),
        pytest.param(
            "de-CH",
            "## 2026.11.1\n\n- Neues Design\n\n"
            "## 2026.10.2\n\n- Fehlerbehebung\n\n"
            "## 2025.12.1\n\n- Nur deutsch",
            id="german_region",
        ),
        pytest.param(
            "fr",
            "## 2026.11.1\n\n"
            "- New design, see [documentation](https://docs.fenecon.de/x)\n"
            "- Home & Commercial: dynamic feed-in limit\n\n"
            "## 2026.10.2\n\n- Bug fix\n\n"
            "## 2025.12.1\n\n- Nur deutsch",
            id="fallback_english_then_german",
        ),
    ],
)
def test_release_notes(language: str, expected: str) -> None:
    """Only changes visible to owners, in the requested or a fallback language."""
    releases = releases_between(parse_changelog(CHANGELOG), "2025.11.3", "2026.11.1")

    assert release_notes(releases, language) == expected


def test_release_notes_without_visible_changes() -> None:
    """Releases with changes only for installers have no release notes."""
    releases = releases_between(parse_changelog(CHANGELOG), "2026.9.3", "2026.9.4")

    assert release_notes(releases, "en") is None


def _make_edge(
    connection_type: str = CONN_TYPE_LOCAL_FEMS, hostname: str = "edge"
) -> OpenEMSEdge:
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_type = connection_type
    backend.connection.conn_url = URL("ws://192.0.2.1:80/websocket")
    return OpenEMSEdge(backend, "0", {"_host": {"Hostname": hostname}})


def _mock_changelog(edge: OpenEMSEdge, data: Any = CHANGELOG) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = AsyncMock(return_value=data)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=None)
    get = MagicMock(return_value=context)
    edge.backend.connection.session.get = get
    return get


def _set_update_state(edge: OpenEMSEdge, installed: str, latest: str) -> None:
    edge.get_system_update_state = AsyncMock(
        return_value=SystemUpdateState(
            status=UpdateStatus.AVAILABLE,
            installed_version=installed,
            latest_version=latest,
        )
    )


@pytest.mark.parametrize(
    ("connection_type", "hostname", "manufacturer", "url", "notes"),
    [
        pytest.param(
            CONN_TYPE_LOCAL_FEMS,
            "edge",
            "FENECON",
            "http://192.0.2.1/_/changelog",
            True,
            id="fems",
        ),
        pytest.param(
            CONN_TYPE_DIRECT_EDGE,
            "openems",
            None,
            "https://github.com/OpenEMS/openems/releases",
            False,
            id="openems",
        ),
    ],
)
def test_edge_release_info(
    connection_type: str,
    hostname: str,
    manufacturer: str | None,
    url: str,
    notes: bool,
) -> None:
    """The edge provides manufacturer, release URL and release notes availability."""
    edge = _make_edge(connection_type, hostname)

    assert edge.manufacturer == manufacturer
    assert edge.release_url == url
    assert edge.has_release_notes is notes


async def test_edge_release_notes() -> None:
    """The release notes cover the update range, the changelog is read once."""
    edge = _make_edge()
    get = _mock_changelog(edge)
    _set_update_state(edge, "2026.10.2", "2026.11.1")

    notes = await edge.get_release_notes("de")
    assert notes == "## 2026.11.1\n\n- Neues Design"
    assert await edge.get_release_notes("en") == (
        "## 2026.11.1\n\n"
        "- New design, see [documentation](https://docs.fenecon.de/x)\n"
        "- Home & Commercial: dynamic feed-in limit"
    )

    get.assert_called_once()
    assert get.call_args.args[0] == FEMS_CHANGELOG_URL


async def test_edge_release_notes_use_known_update_state() -> None:
    """The range is taken from the last read update state with versions."""
    edge = _make_edge()
    _mock_changelog(edge)
    edge.backend.connection.rpc_server.edgeRpc = AsyncMock(
        side_effect=[
            {
                "payload": {
                    "result": {
                        "available": {
                            "currentVersion": "2026.10.2",
                            "latestVersion": "2026.11.1",
                        }
                    }
                }
            },
            {"payload": {"result": {"running": {"percentCompleted": 30}}}},
        ]
    )
    await edge.get_system_update_state()
    # while the update is running, the update state has no versions
    await edge.get_system_update_state()

    assert await edge.get_release_notes("de") == "## 2026.11.1\n\n- Neues Design"


async def test_edge_release_notes_read_again_for_new_version() -> None:
    """A new latest version reads the changelog again."""
    edge = _make_edge()
    get = _mock_changelog(edge)
    _set_update_state(edge, "2026.9.4", "2026.10.2")
    await edge.get_release_notes("de")
    _set_update_state(edge, "2026.10.2", "2026.11.1")

    assert await edge.get_release_notes("de") == "## 2026.11.1\n\n- Neues Design"
    assert get.call_count == 2


async def test_edge_without_release_notes() -> None:
    """OpenEMS edges have no release notes, nothing is read."""
    edge = _make_edge(CONN_TYPE_DIRECT_EDGE, "openems")
    get = _mock_changelog(edge)

    assert await edge.get_release_notes("en") is None
    get.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(aiohttp.ClientConnectionError("offline"), id="offline"),
        pytest.param(TimeoutError(), id="timeout"),
    ],
)
async def test_edge_release_notes_read_error(error: Exception) -> None:
    """Errors while reading the changelog return no release notes."""
    edge = _make_edge()
    edge.backend.connection.session.get = MagicMock(side_effect=error)
    _set_update_state(edge, "2026.10.2", "2026.11.1")

    assert await edge.get_release_notes("en") is None

    with pytest.raises(OpenEMSConnectionError):
        await edge._read_changelog()


async def test_edge_release_notes_without_update_state() -> None:
    """Without a readable update state, there are no release notes."""
    edge = _make_edge()
    edge.get_system_update_state = AsyncMock(
        side_effect=OpenEMSConnectionError("offline")
    )

    assert await edge.get_release_notes("en") is None


async def test_edge_release_notes_during_update() -> None:
    """Without known versions, eg while an update is running, there are none."""
    edge = _make_edge()
    edge.get_system_update_state = AsyncMock(
        return_value=SystemUpdateState(
            status=UpdateStatus.RUNNING, percent_completed=30
        )
    )

    assert await edge.get_release_notes("en") is None
