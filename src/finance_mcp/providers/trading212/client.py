"""Read-only async client for the Trading 212 Public API (v0).

Source of truth: https://docs.trading212.com/_bundle/api.yaml

Guarantees:
- Only GET. The class has no other request method, and its transport refuses
  anything else (see `finance_mcp.http.GetOnlyTransport`).
- Only allowlisted endpoint paths can be requested.
- Per-endpoint client-side throttling from the spec, plus back-off on 429 using
  the `x-ratelimit-*` response headers.
- Short in-memory TTL cache per endpoint, with single-flight to avoid stampedes.
- Credentials never appear in logs or error messages.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from finance_mcp.http import ReadOnlyViolation, get_only_client

log = logging.getLogger(__name__)

BASE_URLS: dict[str, str] = {
    "live": "https://live.trading212.com",
    "demo": "https://demo.trading212.com",
}
API_PREFIX = "/api/v0"

MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0


@dataclass(frozen=True)
class EndpointPolicy:
    name: str
    pattern: re.Pattern[str]
    requests: int  # allowed requests ...
    period: float  # ... per this many seconds (from the spec)
    cache_ttl: float
    scope: str  # API key permission the endpoint needs (for error messages)


def _p(name: str, path: str, requests: int, period: float, ttl: float, scope: str) -> EndpointPolicy:
    regex = "^" + re.escape(API_PREFIX + path).replace(re.escape("{id}"), r"\d+") + "$"
    return EndpointPolicy(name, re.compile(regex), requests, period, ttl, scope)


# Read endpoints only. Rate limits are copied from the OpenAPI spec.
ENDPOINTS: tuple[EndpointPolicy, ...] = (
    _p("account_summary", "/equity/account/summary", 1, 5, 30, "account"),
    _p("positions", "/equity/positions", 1, 1, 30, "portfolio"),
    _p("history_orders", "/equity/history/orders", 20, MINUTE, 5 * MINUTE, "history:orders"),
    _p("history_dividends", "/equity/history/dividends", 20, MINUTE, 5 * MINUTE, "history:dividends"),
    _p("history_transactions", "/equity/history/transactions", 20, MINUTE, 5 * MINUTE, "history:transactions"),
    _p("instruments", "/equity/metadata/instruments", 1, 50, DAY, "metadata"),
    _p("exchanges", "/equity/metadata/exchanges", 1, 30, DAY, "metadata"),
    _p("pies", "/equity/pies", 1, 30, 5 * MINUTE, "pies:read"),
    _p("pie", "/equity/pies/{id}", 1, 5, MINUTE, "pies:read"),
)


def policy_for(path: str) -> EndpointPolicy:
    for policy in ENDPOINTS:
        if policy.pattern.match(path):
            return policy
    raise Trading212Error(f"Endpoint not allowed by this read-only server: {path}")


class Trading212Error(Exception):
    """Error whose message is safe to show to the model and the user."""


class RateLimitedError(Trading212Error):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class _Window:
    """Sliding-window limiter state for one endpoint."""

    stamps: deque[float] = field(default_factory=deque)
    blocked_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RateLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        max_wait: float = 30.0,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._max_wait = max_wait
        self._windows: dict[str, _Window] = {}

    def _window(self, key: str) -> _Window:
        return self._windows.setdefault(key, _Window())

    async def acquire(self, policy: EndpointPolicy) -> None:
        win = self._window(policy.name)
        async with win.lock:
            while True:
                now = self._clock()
                while win.stamps and now - win.stamps[0] >= policy.period:
                    win.stamps.popleft()
                wait = max(win.blocked_until - now, 0.0)
                if len(win.stamps) >= policy.requests:
                    wait = max(wait, win.stamps[0] + policy.period - now)
                if wait <= 0:
                    win.stamps.append(now)
                    return
                if wait > self._max_wait:
                    raise RateLimitedError(
                        f"Trading 212 rate limit for {policy.name} "
                        f"({policy.requests} per {policy.period:g}s); try again in {wait:.0f}s.",
                        retry_after=wait,
                    )
                await self._sleep(wait)

    def block_until(self, policy: EndpointPolicy, until_monotonic: float) -> None:
        win = self._window(policy.name)
        win.blocked_until = max(win.blocked_until, until_monotonic)


class TTLCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic, max_entries: int = 512) -> None:
        self._clock = clock
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def get(self, key: str) -> tuple[bool, Any]:
        hit = self._data.get(key)
        if hit and hit[0] > self._clock():
            return True, hit[1]
        self._data.pop(key, None)
        return False, None

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        if len(self._data) >= self._max:
            now = self._clock()
            for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
                del self._data[k]
            while len(self._data) >= self._max:
                self._data.pop(next(iter(self._data)))
        self._data[key] = (self._clock() + ttl, value)

    def lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def clear(self) -> None:
        self._data.clear()


@dataclass
class Page:
    items: list[Any]
    truncated: bool  # True if more pages existed but max_pages was reached
    pages: int


class Trading212Client:
    """Read-only Trading 212 client. Exposes GET only."""

    def __init__(
        self,
        *,
        env: Literal["live", "demo"],
        api_key: str,
        api_secret: str,
        timeout: float = 15.0,
        max_pages: int = 20,
        max_wait: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        if env not in BASE_URLS:
            raise ValueError("TRADING212_ENV must be 'live' or 'demo'")
        if not api_key or not api_secret:
            raise ValueError("TRADING212_API_KEY and TRADING212_API_SECRET are required")
        self.env = env
        self.max_pages = max_pages
        self._max_wait = max_wait
        self._clock = clock
        self._wall = wall_clock
        self._sleep = sleep
        self._limiter = RateLimiter(clock=clock, sleep=sleep, max_wait=max_wait)
        self._cache = TTLCache(clock=clock)
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._http = get_only_client(
            base_url=BASE_URLS[env],
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    def __repr__(self) -> str:  # never show credentials
        return f"Trading212Client(env={self.env!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def clear_cache(self) -> None:
        self._cache.clear()

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET an allowlisted endpoint and return parsed JSON (cached per endpoint TTL)."""
        policy = policy_for(path)
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        key = str(httpx.URL(path, params=clean))
        hit, value = self._cache.get(key)
        if hit:
            return value
        async with self._cache.lock(key):
            hit, value = self._cache.get(key)
            if hit:
                return value
            value = await self._fetch(policy, path, clean)
            self._cache.set(key, value, policy.cache_ttl)
            return value

    async def get_all_pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int | None = None,
        stop: Callable[[list[Any]], bool] | None = None,
    ) -> Page:
        """Follow `nextPagePath` cursors. `stop(page_items)` returning True ends paging early."""
        cap = max_pages or self.max_pages
        items: list[Any] = []
        next_path: str | None = path
        next_params: dict[str, Any] | None = {"limit": 50, **(params or {})}
        pages = 0
        while next_path and pages < cap:
            body = await self.get(next_path, next_params)
            pages += 1
            page_items = list(body.get("items") or []) if isinstance(body, dict) else []
            items.extend(page_items)
            raw_next = body.get("nextPagePath") if isinstance(body, dict) else None
            if not raw_next or not page_items or (stop and stop(page_items)):
                return Page(items, truncated=False, pages=pages)
            next_path, next_params = self._parse_next(raw_next, path)
        return Page(items, truncated=bool(next_path), pages=pages)

    @staticmethod
    def _parse_next(raw_next: str, original_path: str) -> tuple[str, dict[str, Any]]:
        url = httpx.URL(raw_next)
        # Only follow cursors that stay on the same host and endpoint.
        if url.is_absolute_url or url.path != original_path:
            raise Trading212Error("Trading 212 returned an unexpected pagination link; stopping.")
        return url.path, dict(url.params)

    async def _fetch(self, policy: EndpointPolicy, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(3):
            await self._limiter.acquire(policy)
            try:
                response = await self._http.get(path, params=params)
            except ReadOnlyViolation:
                raise
            except httpx.TimeoutException:
                raise Trading212Error(f"Trading 212 timed out on {policy.name}; try again shortly.") from None
            except httpx.HTTPError as exc:
                raise Trading212Error(f"Could not reach Trading 212 ({type(exc).__name__}) on {policy.name}.") from None

            self._note_rate_headers(policy, response)
            if response.status_code == 429:
                wait = self._retry_after(response, policy)
                log.warning("t212 429 on %s; backing off %.1fs (attempt %d)", policy.name, wait, attempt + 1)
                if wait > self._max_wait or attempt == 2:
                    raise RateLimitedError(
                        f"Trading 212 rate limit hit for {policy.name}; try again in {wait:.0f}s.",
                        retry_after=wait,
                    )
                self._limiter.block_until(policy, self._clock() + wait)
                continue
            return self._handle(policy, response)
        raise AssertionError("unreachable")  # pragma: no cover

    def _note_rate_headers(self, policy: EndpointPolicy, response: httpx.Response) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = _float(response.headers.get("x-ratelimit-reset"))
        if remaining is not None and remaining.strip() == "0" and reset:
            self._limiter.block_until(policy, self._clock() + max(reset - self._wall(), 0.0))

    def _retry_after(self, response: httpx.Response, policy: EndpointPolicy) -> float:
        reset = _float(response.headers.get("x-ratelimit-reset"))
        if reset:
            return max(reset - self._wall(), 0.0) + 0.25
        retry_after = _float(response.headers.get("retry-after"))
        if retry_after is not None:
            return retry_after
        period = _float(response.headers.get("x-ratelimit-period")) or policy.period
        return period / max(policy.requests, 1)

    def _handle(self, policy: EndpointPolicy, response: httpx.Response) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                raise Trading212Error(f"Trading 212 returned invalid JSON for {policy.name}.") from None
        log.info("t212 %s returned HTTP %d", policy.name, status)
        if status == 401:
            raise Trading212Error(
                "Trading 212 rejected the API credentials (401). Check the API key/secret and "
                "that TRADING212_ENV matches the account (live vs demo)."
            )
        if status == 403:
            raise Trading212Error(
                f"The Trading 212 API key is missing the '{policy.scope}' permission (403). "
                "Enable it in Trading 212 > Settings > API."
            )
        if status == 404:
            raise Trading212Error(f"Not found on Trading 212 ({policy.name}).")
        if status == 400:
            raise Trading212Error(f"Trading 212 rejected the request parameters for {policy.name} (400).")
        if status == 408:
            raise Trading212Error(f"Trading 212 timed out on {policy.name} (408); try again shortly.")
        raise Trading212Error(f"Trading 212 error HTTP {status} on {policy.name}; try again later.")


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
