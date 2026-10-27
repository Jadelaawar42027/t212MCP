"""The only place in the codebase that creates outbound HTTP clients.

Every outbound request goes through `GetOnlyTransport`, which refuses any method
other than GET before a byte leaves the process. Tests assert that no other module
constructs an httpx client.
"""

from __future__ import annotations

import logging

import httpx

ALLOWED_METHODS = frozenset({"GET"})

# httpx/httpcore log request lines at DEBUG; keep them quiet so nothing sensitive leaks.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class ReadOnlyViolation(RuntimeError):
    """Raised when code tries to send a non-GET request."""


class GetOnlyTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method.upper() not in ALLOWED_METHODS:
            raise ReadOnlyViolation(f"Blocked outbound {request.method} request: this server is read-only.")
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def get_only_client(
    *,
    base_url: str = "",
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
        transport=GetOnlyTransport(transport),
        follow_redirects=False,
    )
