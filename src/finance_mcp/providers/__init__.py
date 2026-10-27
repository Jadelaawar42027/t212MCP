"""Data providers. Each one owns its client, settings and MCP tools.

To add a provider (e.g. Revolut): create `providers/<name>/`, expose a `from_env()`
that returns a `Provider` (or None when its env vars are unset), and add it to
`load_providers()` below. Tool names must be unique across providers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mcp.server import MCPServer

log = logging.getLogger(__name__)


class Provider(Protocol):
    name: str

    def register_tools(self, mcp: MCPServer) -> None: ...

    async def aclose(self) -> None: ...


def load_providers() -> list[Provider]:
    from finance_mcp.providers import trading212

    providers: list[Provider] = []
    for module in (trading212,):
        provider = module.from_env()
        if provider is None:
            log.warning("provider %s not configured; skipping", module.__name__.rsplit(".", 1)[-1])
            continue
        providers.append(provider)
    return providers
