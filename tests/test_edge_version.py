"""Tests for the software version of the edge."""

from unittest.mock import MagicMock

from yarl import URL

from openems_client.openems import OpenEMSEdge

META = {
    "factoryId": "Core.Meta",
    "properties": {},
    "channels": [
        {"id": "Version", "type": "STRING", "unit": "", "accessMode": "RO"},
    ],
}


def _make_edge(with_meta: bool = True) -> OpenEMSEdge:
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/")
    config: dict = {"_host": {"Hostname": "fems1"}}
    if with_meta:
        config["_meta"] = META
    return OpenEMSEdge(backend, "0", config)


async def test_version_is_read_while_listened() -> None:
    """The version channel is subscribed while there are listeners."""
    edge = _make_edge()
    listener = MagicMock()
    assert edge.version is None
    assert "_meta/Version" not in edge.registered_channels

    remove_listener = edge.add_version_listener(listener)
    assert "_meta/Version" in edge.registered_channels

    edge.currentData({"_meta/Version": "2026.7.2"})
    assert edge.version == "2026.7.2"
    listener.assert_called()

    remove_listener()
    assert "_meta/Version" not in edge.registered_channels


async def test_version_is_kept_while_disconnected() -> None:
    """The last known version stays valid on connection loss, a new one is reported."""
    edge = _make_edge()
    listener = MagicMock()
    edge.add_version_listener(listener)
    edge.currentData({"_meta/Version": "2026.7.2"})

    edge.set_unavailable()
    assert edge.version == "2026.7.2"

    listener.reset_mock()
    edge.currentData({"_meta/Version": "2026.8.1"})
    assert edge.version == "2026.8.1"
    listener.assert_called()


async def test_unchanged_version_does_not_notify() -> None:
    """Listeners are only called for a changed version once the edge is available."""
    edge = _make_edge()
    listener = MagicMock()
    edge.add_version_listener(listener)
    edge.currentData({"_meta/Version": "2026.7.2"})
    listener.reset_mock()

    edge.currentData({"_meta/Version": "2026.7.2"})

    listener.assert_not_called()


async def test_edge_without_meta_component() -> None:
    """Without the _meta component, the version is unknown."""
    edge = _make_edge(with_meta=False)
    remove_listener = edge.add_version_listener(MagicMock())

    assert edge.version is None
    assert not edge.registered_channels
    remove_listener()
