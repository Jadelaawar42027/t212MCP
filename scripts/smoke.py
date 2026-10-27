"""Local end-to-end smoke test against your Trading 212 DEMO account.

Starts the real server on 127.0.0.1, checks /health and that /mcp refuses
unauthenticated calls, mints a short-lived access token directly (the browser
sign-in itself is covered by tests/test_auth.py), then calls every tool over
Streamable HTTP with the official MCP client.

Usage:
    TRADING212_ENV=demo TRADING212_API_KEY=... TRADING212_API_SECRET=... \
        uv run python scripts/smoke.py [--show] [--allow-live]

Reads TRADING212_* from the environment or .env. Refuses TRADING212_ENV=live
unless --allow-live is passed (it is read-only either way).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import socket
import sys
import time

import httpx
import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from finance_mcp.auth import OWNER, SCOPE, OwnerAuthServer
from finance_mcp.config import Settings
from finance_mcp.providers import trading212
from finance_mcp.server import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fail(msg: str) -> None:
    print(f"✗ {msg}")
    sys.exit(1)


async def main(show: bool, allow_live: bool) -> int:
    try:
        t212 = trading212.Trading212Settings()  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        fail("Set TRADING212_API_KEY and TRADING212_API_SECRET (env or .env).")
    if t212.env == "live" and not allow_live:
        fail("TRADING212_ENV=live. This smoke test targets demo; pass --allow-live to override.")
    provider = trading212.from_env()
    assert provider is not None

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        public_base_url=base,
        owner_password=secrets.token_urlsafe(18),
        oauth_signing_key=secrets.token_urlsafe(48),
    )
    auth = OwnerAuthServer(settings)
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings, [provider], auth), host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    print(f"server up at {base} (Trading 212 env: {t212.env})")

    failures = 0
    try:
        async with httpx.AsyncClient(base_url=base) as plain:
            r = await plain.get("/health")
            assert r.status_code == 200 and r.content == b"", r
            print("✓ /health 200, empty body")
            r = await plain.get("/nope")
            assert r.status_code == 404, r
            print("✓ unknown path 404")
            r = await plain.post("/mcp", json={})
            assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", ""), r
            print("✓ /mcp without token 401 with discovery header")

        token = auth.signer.encode(
            "access",
            {"sub": OWNER, "cid": "smoke", "sc": SCOPE, "aud": settings.mcp_url, "fam": "smoke", "jti": "smoke"},
            ttl=900,
        )
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=180) as http,
            Client(streamable_http_client(settings.mcp_url, http_client=http)) as client,
        ):
            tools = await client.list_tools()
            print(f"✓ {len(tools.tools)} tools listed: {', '.join(t.name for t in tools.tools)}")

            async def call(name: str, args: dict) -> dict | None:
                nonlocal failures
                t0 = time.monotonic()
                res = await client.call_tool(name, args)
                text = res.content[0].text if res.content else ""
                ms = (time.monotonic() - t0) * 1000
                if res.is_error:
                    failures += 1
                    print(f"✗ {name}({args}) {ms:.0f}ms: {text}")
                    return None
                print(f"✓ {name}({args}) {ms:.0f}ms, {len(text)} chars")
                if show:
                    print(json.dumps(json.loads(text), indent=2)[:4000])
                return json.loads(text)

            await call("get_account_summary", {})
            positions = await call("get_positions", {})
            if positions and positions["positions"]:
                await call("get_position", {"ticker": positions["positions"][0]["ticker"]})
            await call("portfolio_overview", {"top_n": 3})
            await call("search_instruments", {"query": "AAPL", "limit": 3})
            await call("get_order_history", {"limit": 5})
            await call("get_dividends", {"limit": 5})
            await call("get_transactions", {"limit": 5})
            await call("dividend_report", {"period": "ttm"})
            await call("net_deposits_and_returns", {})
            pies = await call("get_pies", {})
            if pies and pies["pies"]:
                await call("get_pie", {"id": pies["pies"][0]["id"]})
    finally:
        server.should_exit = True
        await task
        await provider.aclose()

    print("\nAll good." if not failures else f"\n{failures} tool call(s) failed (see above).")
    return 1 if failures else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="  %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx2", "httpx", "mcp"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--show", action="store_true", help="print each tool's JSON output")
    parser.add_argument("--allow-live", action="store_true", help="allow TRADING212_ENV=live")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.show, args.allow_live)))
