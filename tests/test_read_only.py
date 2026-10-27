"""The GET-only guarantee. These tests fail if any non-GET outbound request is possible."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import httpx
import pytest

from finance_mcp.http import GetOnlyTransport, ReadOnlyViolation, get_only_client
from finance_mcp.providers.trading212.client import Trading212Client

SRC = Path(__file__).resolve().parents[1] / "src" / "finance_mcp"
WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"]


def test_client_exposes_no_write_methods():
    public = {name for name, _ in inspect.getmembers(Trading212Client) if not name.startswith("_")}
    forbidden = {"post", "put", "patch", "delete", "request", "send", "stream", "head", "options"}
    assert not public & forbidden, public & forbidden


@pytest.mark.parametrize("method", WRITE_METHODS)
async def test_underlying_http_client_refuses_non_get(method, router):
    calls = []
    router.route().mock(side_effect=lambda req: calls.append(req) or httpx.Response(200))
    c = Trading212Client(env="demo", api_key="k", api_secret="s", transport=httpx.MockTransport(router.async_handler))
    with pytest.raises(ReadOnlyViolation):
        await c._http.request(method, "/api/v0/equity/pies")
    assert calls == [], "a non-GET request reached the network layer"
    await c.aclose()


@pytest.mark.parametrize("method", WRITE_METHODS)
async def test_shared_factory_refuses_non_get(method):
    hit = []
    inner = httpx.MockTransport(lambda req: hit.append(req) or httpx.Response(200))
    async with get_only_client(base_url="https://example.com", transport=inner) as c:
        with pytest.raises(ReadOnlyViolation):
            await c.request(method, "/")
        assert (await c.get("/")).status_code == 200
    assert [r.method for r in hit] == ["GET"]


def test_client_uses_get_only_transport():
    c = Trading212Client(env="demo", api_key="k", api_secret="s")
    assert isinstance(c._http._transport, GetOnlyTransport)


def _sources() -> list[tuple[Path, str]]:
    return [(p, p.read_text()) for p in SRC.rglob("*.py")]


def test_no_outbound_write_calls_in_source():
    pattern = re.compile(r"\.(post|put|patch|delete|request|stream|send)\s*\(")
    # Reading an *inbound* Starlette request body is not an outbound HTTP call.
    inbound = re.compile(r"\brequest\.stream\(\)")
    offenders = [
        f"{p.relative_to(SRC)}:{n}: {line.strip()}"
        for p, text in _sources()
        for n, line in enumerate(text.splitlines(), 1)
        if pattern.search(inbound.sub("", line))
    ]
    assert offenders == [], offenders


def test_only_http_module_creates_http_clients():
    pattern = re.compile(r"httpx\.(Async)?Client\s*\(|AsyncHTTPTransport\s*\(|\bimport (requests|aiohttp|urllib3)\b")
    offenders = [str(p.relative_to(SRC)) for p, text in _sources() if pattern.search(text) and p.name != "http.py"]
    assert offenders == [], offenders


def test_no_write_http_method_literals_in_providers():
    pattern = re.compile(r"method\s*=\s*['\"](POST|PUT|PATCH|DELETE)['\"]", re.IGNORECASE)
    offenders = [str(p.relative_to(SRC)) for p, text in _sources() if pattern.search(text)]
    assert offenders == [], offenders
