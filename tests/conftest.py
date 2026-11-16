"""Common fixtures for the openems-client tests."""

from unittest.mock import MagicMock

import pytest
from yarl import URL


@pytest.fixture
def dummy_backend():
    """Minimal mock OpenEMSBackend for unit tests that construct OpenEMSEdge directly."""
    backend = MagicMock()
    backend.multi_edge = False
    backend.connection.conn_url = URL("ws://localhost:8085/openems-backend-ui")
    return backend
