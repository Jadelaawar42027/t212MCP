"""Revolut provider via Enable Banking (read-only, restricted production mode)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from pydantic import AliasChoices, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from finance_mcp.providers.revolut.client import HOUR, EnableBankingClient
from finance_mcp.providers.revolut.keys import JwtSigner, KeyLoadError, load_private_key
from finance_mcp.providers.revolut.tools import RevolutService, register_tools

if TYPE_CHECKING:
    from mcp.server import MCPServer

log = logging.getLogger(__name__)


class RevolutSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ENABLE_BANKING_", env_file=".env", extra="ignore")

    app_id: str = Field(min_length=1)
    key_path: str | None = None
    private_key_b64: SecretStr | None = None
    session_id: str | None = None
    cache_ttl_hours: float = Field(default=6.0, ge=4.0, le=24.0)
    history_days: int = Field(default=90, ge=7, le=730)

    account_labels: dict[str, str] = Field(default_factory=dict, validation_alias=AliasChoices("ACCOUNT_LABELS"))
    owner_names: str = Field(default="", validation_alias=AliasChoices("OWNER_NAMES"))
    living_budget: float = Field(default=1500.0, gt=0, validation_alias=AliasChoices("LIVING_BUDGET"))
    living_account: str = Field(default="Living Expenses Monthly", validation_alias=AliasChoices("LIVING_ACCOUNT"))
    timezone: str = Field(default="Europe/Madrid", validation_alias=AliasChoices("TIMEZONE"))

    @field_validator("account_labels", mode="before")
    @classmethod
    def _labels(cls, value: object) -> object:
        if isinstance(value, str):
            value = json.loads(value) if value.strip() else {}
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise ValueError('ACCOUNT_LABELS must be a JSON object like {"<account id>": "Main"}')
        return value


class RevolutProvider:
    name = "revolut"

    def __init__(self, client: EnableBankingClient, service: RevolutService) -> None:
        self.client = client
        self.service = service

    def register_tools(self, mcp: MCPServer) -> None:
        register_tools(mcp, self.service)

    async def aclose(self) -> None:
        await self.client.aclose()


def from_env() -> RevolutProvider | None:
    try:
        settings = RevolutSettings()  # type: ignore[call-arg]
    except SettingsError:
        log.error("revolut settings invalid: ACCOUNT_LABELS must be a JSON object")
        return None
    except ValidationError as exc:
        fields = {".".join(str(p) for p in e["loc"]) for e in exc.errors()}
        if fields != {"app_id"}:  # app_id missing alone just means "not configured"
            log.error("revolut settings invalid: %s", ", ".join(sorted(fields)))
        return None
    try:
        key = load_private_key(
            key_path=settings.key_path,
            key_b64=settings.private_key_b64.get_secret_value() if settings.private_key_b64 else None,
        )
    except KeyLoadError as exc:
        log.error("revolut disabled: %s", exc)
        return None
    if not settings.session_id:
        log.warning("ENABLE_BANKING_SESSION_ID not set; Revolut tools will ask you to run scripts/link_revolut.py")
    client = EnableBankingClient(
        signer=JwtSigner(settings.app_id, key),
        session_id=settings.session_id,
        cache_ttl=settings.cache_ttl_hours * HOUR,
        history_days=settings.history_days,
    )
    service = RevolutService(
        client,
        labels=settings.account_labels,
        owner_names=[n for n in settings.owner_names.split(",") if n.strip()],
        living_budget=settings.living_budget,
        living_account=settings.living_account,
        tz=settings.timezone,
    )
    return RevolutProvider(client, service)
