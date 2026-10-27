"""HTTP entrypoint: MCP over Streamable HTTP at /mcp, protected by single-owner OAuth.

Routes:
  GET  /health                                   200, empty body
  GET  /.well-known/oauth-authorization-server   OAuth metadata (RFC 8414)
  GET  /.well-known/oauth-protected-resource[/mcp]  resource metadata (RFC 9728)
  POST /register, GET|POST /authorize, POST /token  OAuth endpoints
  *    /mcp                                      MCP (bearer token required)
  everything else                                404
"""

from __future__ import annotations

import ipaddress
import logging
from contextlib import asynccontextmanager

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from finance_mcp import __version__
from finance_mcp.auth import SCOPE, OwnerAuthServer
from finance_mcp.config import Settings
from finance_mcp.providers import Provider, load_providers

log = logging.getLogger("finance_mcp")

INSTRUCTIONS = (
    "Read-only access to the owner's brokerage data (Trading 212). No tool can trade, move money or change "
    "anything. Amounts are in the account currency unless a field says otherwise. Start with "
    "portfolio_overview for portfolio questions, dividend_report for income, net_deposits_and_returns for "
    "overall performance; use search_instruments to resolve tickers like AAPL_US_EQ."
)


class SecurityMiddleware:
    """Adds security headers everywhere and (optionally) restricts /mcp to allowed CIDRs."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.networks = settings.allowed_networks
        self.hsts = settings.public_base_url.startswith("https://")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if self.networks and scope["path"].startswith("/mcp") and not self._allowed(scope):
            await PlainTextResponse("Not Found", status_code=404)(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000"))
                headers += [(k, v) for k, v in extra if k not in names]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _allowed(self, scope: Scope) -> bool:
        ip = None
        for key, value in scope.get("headers", []):
            if key == b"x-real-ip":
                ip = value.decode("latin-1").strip()
        if ip is None and scope.get("client"):
            ip = scope["client"][0]
        try:
            addr = ipaddress.ip_address(ip or "")
        except ValueError:
            return False
        return any(addr in net for net in self.networks)


async def health(_: Request) -> Response:
    return Response(status_code=200)


def create_app(
    settings: Settings | None = None,
    providers: list[Provider] | None = None,
    auth_server: OwnerAuthServer | None = None,
) -> ASGIApp:
    settings = settings or Settings()  # type: ignore[call-arg]
    providers = load_providers() if providers is None else providers
    auth_server = auth_server or OwnerAuthServer(settings)

    mcp = MCPServer(
        name="finance-mcp",
        title="Personal finance (read-only)",
        instructions=INSTRUCTIONS,
        version=__version__,
        token_verifier=auth_server,
        auth=AuthSettings(
            issuer_url=settings.public_base_url,
            resource_server_url=settings.mcp_url,
            required_scopes=[SCOPE],
            validate_token_resource=True,
        ),
    )
    for provider in providers:
        provider.register_tools(mcp)
        log.info("registered provider %s", provider.name)

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",  # noqa: S104 - container binds all interfaces; Host header is still checked
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[settings.public_host, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[settings.public_base_url, "https://claude.ai", "https://claude.com"],
        ),
    )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
        for provider in providers:
            await provider.aclose()

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET", "HEAD"]),
            *auth_server.routes(),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
    return SecurityMiddleware(app, settings)


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
