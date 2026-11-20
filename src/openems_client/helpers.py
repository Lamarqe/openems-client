"""Helper methods which are independent of openems and HA classes."""

import asyncio
import contextlib
import re
from typing import Any
import uuid

from yarl import URL

from .const import CONN_TYPES, ConnectionType

SNAKE_REPLACE_PATTERN = re.compile(r"[^-a-zA-Z0-9]")


def connection_url(type: str, host: str | None = None) -> URL:
    "Construct URL for the given type and host."
    default_params: ConnectionType = CONN_TYPES[type]
    if not (url_host := default_params["host"]):
        url_host = host
    return URL.build(
        scheme=default_params["scheme"],
        host="" if url_host is None else url_host,
        port=default_params["port"],
        path=default_params["path"],
    )


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel the task and wait until it has finished."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def wrap_jsonrpc(method: str, **params: Any) -> dict[str, Any]:
    """Wrap a method call with parameters into a jsonrpc call."""
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": str(uuid.uuid4()),
    }


def to_snake_case(name: str) -> str:
    """Convert given name to snake_case."""
    name = SNAKE_REPLACE_PATTERN.sub("_", name).lower()
    return re.sub(r"[_-]{2,}", "_", name).strip("-_")
