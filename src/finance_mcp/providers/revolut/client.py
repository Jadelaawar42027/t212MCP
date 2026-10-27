"""Read-only async client for Enable Banking's account-information API (Revolut).

Source of truth: https://enablebanking.com/docs/api/reference/

Guarantees:
- Only GET. The class has no other request method and its transport refuses anything
  else (see `finance_mcp.http.GetOnlyTransport`). The POSTs needed to create a consent
  (/auth, /sessions) live only in scripts/link_revolut.py, never in the server.
- Only allowlisted paths: this session, and account details/balances/transactions.
- PSD2 background limit: banks allow about 4 unattended pulls per account, per endpoint,
  per 24h. Balances and transactions are cached (default 6h) and a per-account, per-endpoint
  budget of 4 pulls per rolling 24h is enforced here. When the budget is used up or the
  bank answers 429, the last cached data is returned marked `stale` instead of failing.
- The private key and JWTs never appear in logs or error messages.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from finance_mcp.http import ReadOnlyViolation, get_only_client
from finance_mcp.providers.revolut.keys import JwtSigner

log = logging.getLogger(__name__)

BASE_URL = "https://api.enablebanking.com"
HOUR = 3600.0
DAY = 86400.0

PULLS_PER_DAY = 4  # PSD2 RTS art. 36(5): unattended access, per account and endpoint
RATE_LIMIT_BACKOFF = 6 * HOUR  # Enable Banking's advice after ASPSP_RATE_LIMIT_EXCEEDED
SESSION_TTL = HOUR  # served by Enable Banking itself, not by the bank
DETAILS_TTL = DAY

_UID = r"[A-Za-z0-9-]{1,64}"
_ACCOUNT_PATH = re.compile(rf"^/accounts/({_UID})/(details|balances|transactions)$")

RELINK_HINT = (
    "Re-link with `uv run python scripts/link_revolut.py`, then set the new ENABLE_BANKING_SESSION_ID "
    "(locally in .env, and on Railway) and restart."
)


class EnableBankingError(Exception):
    """Error whose message is safe to show to the model and the user."""


class ConsentExpiredError(EnableBankingError):
    pass


class RateLimitedError(EnableBankingError):
    pass


@dataclass
class Entry:
    value: Any
    fetched_at: float  # wall clock
    date_from: date | None = None  # transactions only: earliest day this entry covers
    truncated: bool = False


@dataclass
class Fetched:
    """A value plus how fresh it is, so tools can tell the user."""

    value: Any
    fetched_at: float
    stale: bool = False
    note: str | None = None
    truncated: bool = False

    def meta(self) -> dict:
        out: dict[str, Any] = {"fetched_at": datetime.fromtimestamp(self.fetched_at, UTC).isoformat()}
        if self.stale:
            out["stale"] = True
        if self.note:
            out["note"] = self.note
        if self.truncated:
            out["truncated_history"] = True
        return out


@dataclass
class _Budget:
    stamps: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0


class EnableBankingClient:
    """Read-only Enable Banking client bound to one consent session. Exposes GET only."""

    def __init__(
        self,
        *,
        signer: JwtSigner,
        session_id: str | None,
        cache_ttl: float = 6 * HOUR,
        history_days: int = 90,
        max_pages: int = 30,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.session_id = session_id or None
        if self.session_id and not re.fullmatch(_UID, self.session_id):
            raise ValueError("ENABLE_BANKING_SESSION_ID has an unexpected format")
        self.cache_ttl = cache_ttl
        self.history_days = history_days
        self.max_pages = max_pages
        self._signer = signer
        self._clock = clock
        self._entries: dict[str, Entry] = {}
        self._budgets: dict[str, _Budget] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._http = get_only_client(
            base_url=BASE_URL, headers={"Accept": "application/json"}, timeout=timeout, transport=transport
        )

    def __repr__(self) -> str:  # never show credentials
        return f"EnableBankingClient(app_id={self._signer.app_id!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def clear_cache(self) -> None:
        self._entries.clear()

    def today(self) -> date:
        return datetime.fromtimestamp(self._clock(), UTC).date()

    # ------------------------------------------------------------ public reads

    async def session(self) -> Fetched:
        """GET /sessions/{id}: consent validity and the account UIDs it covers."""
        sid = self._require_session()
        return await self._cached(f"session:{sid}", None, SESSION_TTL, lambda: self._get(f"/sessions/{sid}"))

    async def account_details(self, uid: str) -> Fetched:
        return await self._cached(
            f"details:{uid}", f"details:{uid}", DETAILS_TTL, lambda: self._get(f"/accounts/{uid}/details")
        )

    async def balances(self, uid: str) -> Fetched:
        return await self._cached(
            f"balances:{uid}", f"balances:{uid}", self.cache_ttl, lambda: self._get(f"/accounts/{uid}/balances")
        )

    async def transactions(self, uid: str, date_from: date | None = None) -> Fetched:
        """All transactions from `date_from` (default: `history_days` ago) to today.

        One pull covers the whole window and later calls for any later start date reuse it,
        so a few tool calls a day stay within the bank's 4-pulls budget.
        """
        default_from = self.today() - timedelta(days=self.history_days)
        start = min(date_from, default_from) if date_from else default_from

        async def fetch() -> Entry:
            items, truncated = await self._all_transactions(uid, start)
            return Entry(items, self._clock(), date_from=start, truncated=truncated)

        return await self._cached(
            f"transactions:{uid}",
            f"transactions:{uid}",
            self.cache_ttl,
            fetch,
            covers=lambda e: e.date_from is not None and e.date_from <= start,
        )

    # ------------------------------------------------------------ caching + PSD2 budget

    async def _cached(
        self,
        key: str,
        budget_key: str | None,
        ttl: float,
        fetch: Callable[[], Awaitable[Any]],
        covers: Callable[[Entry], bool] = lambda _: True,
    ) -> Fetched:
        async with self._locks.setdefault(key, asyncio.Lock()):
            now = self._clock()
            entry = self._entries.get(key)
            usable = entry if entry is not None and covers(entry) else None
            if usable and now - usable.fetched_at < ttl:
                return Fetched(usable.value, usable.fetched_at, truncated=usable.truncated)

            if budget_key:
                wait = self._budget_wait(budget_key, now)
                if wait > 0:
                    return self._stale_or_raise(usable, wait, "the 4-per-day bank limit for this account")

            try:
                result = await fetch()
            except RateLimitedError:
                if budget_key:
                    self._budget(budget_key).blocked_until = now + RATE_LIMIT_BACKOFF
                return self._stale_or_raise(usable, RATE_LIMIT_BACKOFF, "Revolut's rate limit (HTTP 429)")
            new = result if isinstance(result, Entry) else Entry(result, self._clock())
            if budget_key:
                self._budget(budget_key).stamps.append(now)
            self._entries[key] = new
            return Fetched(new.value, new.fetched_at, truncated=new.truncated)

    def _budget(self, key: str) -> _Budget:
        return self._budgets.setdefault(key, _Budget())

    def _budget_wait(self, key: str, now: float) -> float:
        b = self._budget(key)
        while b.stamps and now - b.stamps[0] >= DAY:
            b.stamps.popleft()
        wait = max(b.blocked_until - now, 0.0)
        if len(b.stamps) >= PULLS_PER_DAY:
            wait = max(wait, b.stamps[0] + DAY - now)
        return wait

    @staticmethod
    def _stale_or_raise(entry: Entry | None, wait: float, reason: str) -> Fetched:
        hours = wait / HOUR
        if entry is not None:
            return Fetched(
                entry.value,
                entry.fetched_at,
                stale=True,
                truncated=entry.truncated,
                note=f"Showing cached data: refresh paused by {reason}; next refresh in about {hours:.1f}h.",
            )
        raise RateLimitedError(f"Revolut data is paused by {reason}; try again in about {hours:.1f}h.")

    # ------------------------------------------------------------ HTTP

    def _require_session(self) -> str:
        if not self.session_id:
            raise EnableBankingError("No Revolut consent yet: ENABLE_BANKING_SESSION_ID is not set. " + RELINK_HINT)
        return self.session_id

    def _check_path(self, path: str) -> str:
        if self.session_id and path == f"/sessions/{self.session_id}":
            return "session"
        m = _ACCOUNT_PATH.match(path)
        if m:
            return m.group(2)
        raise EnableBankingError(f"Endpoint not allowed by this read-only server: {path}")

    async def _all_transactions(self, uid: str, start: date) -> tuple[list[dict], bool]:
        path = f"/accounts/{uid}/transactions"
        params: dict[str, Any] = {"date_from": start.isoformat()}
        items: list[dict] = []
        for _ in range(self.max_pages):
            body = await self._get(path, params)
            if not isinstance(body, dict):
                raise EnableBankingError("Unexpected transactions response from Enable Banking.")
            items.extend(t for t in body.get("transactions") or [] if isinstance(t, dict))
            key = body.get("continuation_key")
            if not key:
                return items, False
            params = {"date_from": start.isoformat(), "continuation_key": key}
        return items, True

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        name = self._check_path(path)
        headers = {"Authorization": f"Bearer {self._signer.token()}"}
        try:
            response = await self._http.get(path, params=params, headers=headers)
        except ReadOnlyViolation:
            raise
        except httpx.TimeoutException:
            raise EnableBankingError(f"Enable Banking timed out on {name}; try again shortly.") from None
        except httpx.HTTPError as exc:
            raise EnableBankingError(f"Could not reach Enable Banking ({type(exc).__name__}) on {name}.") from None
        return self._handle(name, response)

    @staticmethod
    def _handle(name: str, response: httpx.Response) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            try:
                return response.json()
            except ValueError:
                raise EnableBankingError(f"Enable Banking returned invalid JSON for {name}.") from None
        code = _error_code(response)
        log.info("enable banking %s returned HTTP %d %s", name, status, code or "")
        suffix = f" {code}" if code else ""
        if code in ("EXPIRED_SESSION", "CLOSED_SESSION", "REVOKED_SESSION"):
            raise ConsentExpiredError("The Revolut consent has expired or was revoked. " + RELINK_HINT)
        if status == 429 or code == "ASPSP_RATE_LIMIT_EXCEEDED":
            raise RateLimitedError("Revolut rate limit exceeded.")
        if status == 401:
            raise EnableBankingError(
                f"Enable Banking rejected the request (401{suffix}). Check ENABLE_BANKING_APP_ID and that the "
                "private key belongs to that application; if they are right, the consent may have ended "
                "(see consent_status)."
            )
        if status == 403:
            raise EnableBankingError(f"Enable Banking denied access to {name} (403{suffix}).")
        if status == 404:
            if name == "session":
                raise ConsentExpiredError("Enable Banking does not know this session. " + RELINK_HINT)
            raise EnableBankingError(f"Not found on Enable Banking ({name}).")
        if code in ("ASPSP_ERROR", "ASPSP_TIMEOUT") or status >= 500:
            raise EnableBankingError(f"Revolut/Enable Banking error on {name} (HTTP {status}); try again later.")
        raise EnableBankingError(f"Enable Banking error HTTP {status}{suffix} on {name}.")


def _error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{2,64}", code) else None
