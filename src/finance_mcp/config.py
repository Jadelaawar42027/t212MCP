"""Server-wide settings, read from environment variables only."""

from __future__ import annotations

import ipaddress
from functools import cached_property
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Core settings for the HTTP server and its OAuth authorization server."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # Public HTTPS origin of this deployment, e.g. https://finance-mcp.up.railway.app
    public_base_url: str

    # The single owner who may sign in.
    owner_password: SecretStr = Field(min_length=12)
    owner_totp_secret: SecretStr | None = None

    # HMAC key for access/refresh tokens, auth codes and client IDs. 32+ random bytes.
    oauth_signing_key: SecretStr = Field(min_length=32)

    access_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    refresh_token_ttl_seconds: int = Field(default=30 * 86400, ge=3600, le=180 * 86400)

    # Optional comma-separated CIDRs allowed to call /mcp (e.g. Anthropic's 160.79.104.0/21).
    allowed_cidrs: str = ""
    # Accept Claude Code's loopback redirect (http://localhost:<any>/callback).
    allow_loopback_redirects: bool = True
    # Hosts allowed to serve Client ID Metadata Documents.
    cimd_allowed_hosts: str = "claude.ai"

    port: int = 8000
    log_level: str = "INFO"

    @field_validator("public_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        value = value.rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in ("https", "http") or not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("PUBLIC_BASE_URL must be an origin like https://example.up.railway.app")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("PUBLIC_BASE_URL must use https outside localhost")
        return value

    @field_validator("allowed_cidrs")
    @classmethod
    def _check_cidrs(cls, value: str) -> str:
        for part in _split(value):
            ipaddress.ip_network(part, strict=False)
        return value

    @property
    def mcp_url(self) -> str:
        return f"{self.public_base_url}/mcp"

    @property
    def public_host(self) -> str:
        return urlparse(self.public_base_url).netloc

    @cached_property
    def allowed_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(p, strict=False) for p in _split(self.allowed_cidrs)]

    @cached_property
    def cimd_hosts(self) -> frozenset[str]:
        return frozenset(h.lower() for h in _split(self.cimd_allowed_hosts))


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]
