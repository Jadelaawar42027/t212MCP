"""Trading 212 provider (read-only)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from finance_mcp.providers.trading212.client import Trading212Client
from finance_mcp.providers.trading212.tools import Trading212Service, register_tools

if TYPE_CHECKING:
    from mcp.server import MCPServer


class Trading212Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADING212_", env_file=".env", extra="ignore")

    env: Literal["live", "demo"] = "demo"
    api_key: SecretStr
    api_secret: SecretStr
    timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    max_pages: int = Field(default=20, ge=1, le=200)


class Trading212Provider:
    name = "trading212"

    def __init__(self, client: Trading212Client) -> None:
        self.client = client
        self.service = Trading212Service(client)

    def register_tools(self, mcp: MCPServer) -> None:
        register_tools(mcp, self.service)

    async def aclose(self) -> None:
        await self.client.aclose()


def from_env() -> Trading212Provider | None:
    try:
        settings = Trading212Settings()  # type: ignore[call-arg]
    except ValidationError:
        return None
    client = Trading212Client(
        env=settings.env,
        api_key=settings.api_key.get_secret_value(),
        api_secret=settings.api_secret.get_secret_value(),
        timeout=settings.timeout_seconds,
        max_pages=settings.max_pages,
    )
    return Trading212Provider(client)
