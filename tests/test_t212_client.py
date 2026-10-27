from __future__ import annotations

import base64
import logging

import httpx
import pytest
import respx

from finance_mcp.providers.trading212.client import (
    ENDPOINTS,
    RateLimitedError,
    Trading212Client,
    Trading212Error,
    policy_for,
)
from tests.conftest import API_KEY, API_SECRET, BASE, POSITIONS, SUMMARY

SUMMARY_PATH = "/api/v0/equity/account/summary"
POSITIONS_PATH = "/api/v0/equity/positions"
ORDERS_PATH = "/api/v0/equity/history/orders"


async def test_sends_basic_auth_and_uses_env_base_url(client, router):
    route = router.get(SUMMARY_PATH).respond(json=SUMMARY)
    assert await client.get(SUMMARY_PATH) == SUMMARY
    request = route.calls.last.request
    assert str(request.url).startswith(BASE)
    expected = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.method == "GET"


def test_live_and_demo_base_urls():
    for env, host in (("live", "live.trading212.com"), ("demo", "demo.trading212.com")):
        c = Trading212Client(env=env, api_key="k", api_secret="s")
        assert c._http.base_url.host == host
    with pytest.raises(ValueError):
        Trading212Client(env="prod", api_key="k", api_secret="s")  # type: ignore[arg-type]


def test_repr_hides_credentials():
    c = Trading212Client(env="demo", api_key=API_KEY, api_secret=API_SECRET)
    assert API_KEY not in repr(c) and API_SECRET not in repr(c)


async def test_cache_hit_within_ttl_and_refetch_after(client, router, clock):
    route = router.get(SUMMARY_PATH).respond(json=SUMMARY)
    await client.get(SUMMARY_PATH)
    await client.get(SUMMARY_PATH)
    assert route.call_count == 1
    clock.t += 31  # summary TTL is 30s
    await client.get(SUMMARY_PATH)
    assert route.call_count == 2


async def test_cache_keyed_by_params(client, router):
    route = router.get(POSITIONS_PATH).respond(json=POSITIONS[:1])
    await client.get(POSITIONS_PATH, {"ticker": "AAPL_US_EQ"})
    await client.get(POSITIONS_PATH, {"ticker": "SAP_DE_EQ"})
    assert route.call_count == 2


async def test_unknown_endpoint_rejected(client):
    with pytest.raises(Trading212Error, match="not allowed"):
        await client.get("/api/v0/equity/orders")
    with pytest.raises(Trading212Error):
        policy_for("/api/v0/equity/pies/abc")


def test_rate_limits_match_spec():
    limits = {p.name: (p.requests, p.period) for p in ENDPOINTS}
    assert limits == {
        "account_summary": (1, 5),
        "positions": (1, 1),
        "history_orders": (20, 60),
        "history_dividends": (20, 60),
        "history_transactions": (20, 60),
        "instruments": (1, 50),
        "exchanges": (1, 30),
        "pies": (1, 30),
        "pie": (1, 5),
    }
    ttl = {p.name: p.cache_ttl for p in ENDPOINTS}
    assert ttl["instruments"] == 86400 and ttl["positions"] == 30


# ---------------------------------------------------------------- pagination


async def test_pagination_follows_next_page_path(client, router):
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        assert request.url.params["limit"] == "50"
        if cursor is None:
            return httpx.Response(
                200, json={"items": [{"n": 1}, {"n": 2}], "nextPagePath": f"{ORDERS_PATH}?limit=50&cursor=200"}
            )
        if cursor == "200":
            return httpx.Response(200, json={"items": [{"n": 3}], "nextPagePath": f"{ORDERS_PATH}?limit=50&cursor=100"})
        return httpx.Response(200, json={"items": [{"n": 4}], "nextPagePath": None})

    route = router.get(ORDERS_PATH).mock(side_effect=handler)
    page = await client.get_all_pages(ORDERS_PATH)
    assert [i["n"] for i in page.items] == [1, 2, 3, 4]
    assert page.pages == 3 and page.truncated is False
    assert route.call_count == 3


async def test_pagination_respects_max_pages(client, router):
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(
            200, json={"items": [{"n": counter["n"]}], "nextPagePath": f"{ORDERS_PATH}?limit=50&cursor={counter['n']}"}
        )

    router.get(ORDERS_PATH).mock(side_effect=handler)
    page = await client.get_all_pages(ORDERS_PATH, max_pages=3)
    assert page.pages == 3 and page.truncated is True and len(page.items) == 3


async def test_pagination_stop_callback(client, router):
    route = router.get(ORDERS_PATH).respond(
        json={"items": [{"n": 1}], "nextPagePath": f"{ORDERS_PATH}?limit=50&cursor=1"}
    )
    page = await client.get_all_pages(ORDERS_PATH, stop=lambda items: True)
    assert route.call_count == 1 and page.truncated is False


@pytest.mark.parametrize(
    "next_path", ["https://evil.example.com/api/v0/equity/history/orders?cursor=1", "/api/v0/equity/positions?x=1"]
)
async def test_pagination_rejects_foreign_links(client, router, next_path):
    router.get(ORDERS_PATH).respond(json={"items": [{"n": 1}], "nextPagePath": next_path})
    with pytest.raises(Trading212Error, match="pagination"):
        await client.get_all_pages(ORDERS_PATH)


# ---------------------------------------------------------------- rate limiting


async def test_client_side_throttle_waits_between_calls(client, router, clock):
    router.get(POSITIONS_PATH).respond(json=[])
    await client.get(POSITIONS_PATH)
    client.clear_cache()
    await client.get(POSITIONS_PATH)
    assert clock.sleeps == [pytest.approx(1.0)]


async def test_throttle_raises_when_wait_too_long(router, clock):
    c = Trading212Client(
        env="demo",
        api_key="k",
        api_secret="s",
        transport=httpx.MockTransport(router.async_handler),
        clock=clock,
        wall_clock=clock.wall,
        sleep=clock.sleep,
        max_wait=10,
    )
    router.get("/api/v0/equity/metadata/instruments").respond(json=[])
    await c.get("/api/v0/equity/metadata/instruments")
    c.clear_cache()
    with pytest.raises(RateLimitedError, match="try again in 50s"):
        await c.get("/api/v0/equity/metadata/instruments")
    await c.aclose()


async def test_429_backs_off_until_reset_then_retries(client, router, clock):
    reset = clock.wall() + 4
    route = router.get(SUMMARY_PATH).mock(
        side_effect=[
            httpx.Response(429, headers={"x-ratelimit-reset": str(reset), "x-ratelimit-remaining": "0"}),
            httpx.Response(200, json=SUMMARY),
        ]
    )
    assert await client.get(SUMMARY_PATH) == SUMMARY
    assert route.call_count == 2
    assert sum(clock.sleeps) >= 4


async def test_429_with_long_reset_fails_fast(client, router, clock):
    router.get(SUMMARY_PATH).respond(429, headers={"x-ratelimit-reset": str(clock.wall() + 600)})
    with pytest.raises(RateLimitedError) as info:
        await client.get(SUMMARY_PATH)
    assert info.value.retry_after > 500
    assert clock.sleeps == []


async def test_remaining_zero_header_blocks_next_call(client, router, clock):
    router.get(ORDERS_PATH).mock(
        side_effect=[
            httpx.Response(
                200,
                json={"items": [], "nextPagePath": None},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(clock.wall() + 7)},
            ),
            httpx.Response(200, json={"items": [], "nextPagePath": None}),
        ]
    )
    await client.get(ORDERS_PATH, {"cursor": 1})
    await client.get(ORDERS_PATH, {"cursor": 2})
    assert sum(clock.sleeps) == pytest.approx(7)


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("status", "match"),
    [
        (401, "rejected the API credentials"),
        (403, "'account' permission"),
        (404, "Not found"),
        (408, "timed out"),
        (500, "HTTP 500"),
    ],
)
async def test_error_messages_are_clean(client, router, status, match, caplog):
    caplog.set_level(logging.DEBUG)
    router.get(SUMMARY_PATH).respond(status, text="upstream body")
    with pytest.raises(Trading212Error, match=match) as info:
        await client.get(SUMMARY_PATH)
    token = base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()
    for secret in (API_KEY, API_SECRET, token):
        assert secret not in str(info.value)
        assert secret not in caplog.text


async def test_timeout_becomes_clean_error(client, router):
    router.get(SUMMARY_PATH).mock(side_effect=httpx.ReadTimeout("boom"))
    with pytest.raises(Trading212Error, match="timed out"):
        await client.get(SUMMARY_PATH)


async def test_network_error_becomes_clean_error(client, router):
    router.get(SUMMARY_PATH).mock(side_effect=httpx.ConnectError("nope"))
    with pytest.raises(Trading212Error, match="Could not reach"):
        await client.get(SUMMARY_PATH)


async def test_respx_global_mock_also_works():
    """The default transport path (real AsyncHTTPTransport) is mockable with respx too."""
    with respx.mock(base_url=BASE) as mock:
        mock.get(SUMMARY_PATH).respond(json=SUMMARY)
        c = Trading212Client(env="demo", api_key="k", api_secret="s")
        assert (await c.get(SUMMARY_PATH))["currency"] == "GBP"
        await c.aclose()
