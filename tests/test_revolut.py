"""Revolut provider (Enable Banking): keys/JWT, client caching + PSD2 budget, pagination, tools."""

from __future__ import annotations

import base64
import logging
from datetime import UTC, date, datetime

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from finance_mcp.providers import revolut
from finance_mcp.providers.revolut import categorize as cat
from finance_mcp.providers.revolut.client import (
    DAY,
    HOUR,
    ConsentExpiredError,
    EnableBankingClient,
    EnableBankingError,
    RateLimitedError,
)
from finance_mcp.providers.revolut.keys import JwtSigner, KeyLoadError, load_private_key
from finance_mcp.providers.revolut.tools import RevolutService, _run

API = "https://api.enablebanking.com"
APP_ID = "aaaaaaaa-1111-2222-3333-bbbbbbbbbbbb"
SID = "5e55105e-0000-4000-8000-000000000001"
MAIN, POCKET, USD = "acc-main-0001", "acc-pocket-0002", "acc-usd-0003"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # Tuesday


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def pem(rsa_key) -> bytes:
    return rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


class Wall:
    def __init__(self) -> None:
        self.t = NOW.timestamp()

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def wall() -> Wall:
    return Wall()


@pytest.fixture
def router() -> respx.Router:
    return respx.Router(base_url=API, assert_all_mocked=True, assert_all_called=False)


@pytest.fixture
async def client(router, wall, rsa_key):
    c = EnableBankingClient(
        signer=JwtSigner(APP_ID, rsa_key, clock=wall),
        session_id=SID,
        transport=httpx.MockTransport(router.async_handler),
        clock=wall,
    )
    yield c
    await c.aclose()


@pytest.fixture
def service(client) -> RevolutService:
    return RevolutService(
        client,
        labels={"hash-main": "Main", "hash-pocket": "Bills pocket"},
        owner_names=["Jad Test"],
        now=lambda: datetime.fromtimestamp(client._clock(), UTC),
    )


# ---------------------------------------------------------------- sample data

SESSION = {
    "status": "AUTHORIZED",
    "accounts": [MAIN, POCKET, USD],
    "aspsp": {"name": "Revolut", "country": "ES"},
    "access": {"valid_until": "2027-04-20T10:00:00+00:00"},
}
DETAILS = {
    MAIN: {
        "uid": MAIN,
        "name": "EUR Current",
        "currency": "EUR",
        "cash_account_type": "CACC",
        "account_id": {"iban": "LT123250000000000001"},
        "identification_hash": "hash-main",
    },
    POCKET: {"uid": POCKET, "name": "Bills", "currency": "EUR", "identification_hash": "hash-pocket"},
    USD: {"uid": USD, "name": "USD", "currency": "USD", "identification_hash": "hash-usd"},
}


def bal(type_: str, amount: str, ccy: str = "EUR") -> dict:
    return {"balance_type": type_, "balance_amount": {"amount": amount, "currency": ccy}, "name": type_}


def tx(amount: str, ind: str, day: str, *, creditor=None, debtor=None, info=None, mcc=None, status="BOOK", ccy="EUR"):
    t = {
        "transaction_amount": {"amount": amount, "currency": ccy},
        "credit_debit_indicator": ind,
        "booking_date": day,
        "status": status,
        "remittance_information": info or [],
        "entry_reference": f"{day}-{amount}-{creditor or debtor}",
    }
    if creditor:
        t["creditor"] = {"name": creditor}
    if debtor:
        t["debtor"] = {"name": debtor}
    if mcc:
        t["merchant_category_code"] = mcc
    return t


MAIN_TXS = [
    tx("42.10", "DBIT", "2026-10-26", creditor="MERCADONA SA"),
    tx("12.50", "DBIT", "2026-10-26", creditor="Uber Eats"),
    tx("8.00", "DBIT", "2026-10-25", creditor="Uber BV"),
    tx("15.99", "DBIT", "2026-10-20", creditor="Netflix.com"),
    tx("60.00", "DBIT", "2026-10-15", creditor="Some Shop", mcc="5651"),
    tx("300.00", "DBIT", "2026-10-10", info=["To pocket EUR Bills"]),
    tx("500.00", "DBIT", "2026-10-05", creditor="Trading 212 Markets Ltd"),
    tx("2500.00", "CRDT", "2026-10-01", debtor="ACME SL", info=["Salary October"]),
    tx("9.99", "DBIT", "2026-10-02", creditor="AMZN Mktp ES*1A2B3"),
    tx("19.99", "DBIT", "2026-10-03", creditor="AMZN Mktp ES*9Z8Y7"),
    tx("5.00", "DBIT", "2026-10-26", creditor="Rejected Cafe", status="RJCT"),
    tx("100.00", "DBIT", "2026-10-04", creditor="Jad Test", info=["Transfer to own bank"]),
    tx("30.00", "DBIT", "2026-09-20", creditor="Old Restaurant"),
]


def mock_basics(router: respx.Router, txs: dict[str, list] | None = None):
    router.get(f"/sessions/{SID}").mock(return_value=httpx.Response(200, json=SESSION))
    for uid, d in DETAILS.items():
        router.get(f"/accounts/{uid}/details").mock(return_value=httpx.Response(200, json=d))
    txs = txs if txs is not None else {MAIN: MAIN_TXS, POCKET: [], USD: []}
    routes = {}
    for uid, items in txs.items():
        routes[uid] = router.get(f"/accounts/{uid}/transactions").mock(
            return_value=httpx.Response(200, json={"transactions": items, "continuation_key": None})
        )
    return routes


# ---------------------------------------------------------------- keys + JWT


def test_jwt_has_enable_banking_claims(rsa_key, wall):
    token = JwtSigner(APP_ID, rsa_key, clock=wall).token()
    header = jwt.get_unverified_header(token)
    assert header["kid"] == APP_ID and header["alg"] == "RS256"
    claims = jwt.decode(
        token,
        rsa_key.public_key(),
        algorithms=["RS256"],
        audience="api.enablebanking.com",
        options={"verify_exp": False},
    )
    assert claims["iss"] == "enablebanking.com"
    assert claims["exp"] - claims["iat"] <= 86400


def test_jwt_reused_then_renewed(rsa_key, wall):
    s = JwtSigner(APP_ID, rsa_key, clock=wall)
    first = s.token()
    wall.t += 60
    assert s.token() == first
    wall.t += HOUR
    assert s.token() != first
    assert APP_ID in repr(s) and "PRIVATE" not in repr(s)


def test_key_path_wins_over_b64(tmp_path, pem):
    other = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    key = load_private_key(key_path=path, key_b64=base64.b64encode(other).decode())
    assert isinstance(key, rsa.RSAPrivateKey)


def test_key_from_b64_with_newlines(pem):
    b64 = base64.encodebytes(pem).decode()  # wrapped at 76 chars
    assert isinstance(load_private_key(key_b64=b64), rsa.RSAPrivateKey)


@pytest.mark.parametrize(
    ("kwargs", "msg"),
    [
        ({}, "Set ENABLE_BANKING_KEY_PATH"),
        ({"key_b64": "not base64!!"}, "not valid base64"),
        ({"key_b64": base64.b64encode(b"garbage").decode()}, "PEM private key"),
        ({"key_path": "/nonexistent/app.pem"}, "Cannot read"),
    ],
)
def test_key_errors_are_clear_and_leak_nothing(kwargs, msg):
    with pytest.raises(KeyLoadError, match=msg) as exc:
        load_private_key(**kwargs)
    assert "garbage" not in str(exc.value) and "Z2FyYmFnZQ" not in str(exc.value)


def test_from_env_prefers_path_and_parses_labels(monkeypatch, tmp_path, pem):
    path = tmp_path / "k.pem"
    path.write_bytes(pem)
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", APP_ID)
    monkeypatch.setenv("ENABLE_BANKING_KEY_PATH", str(path))
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_B64", "bm90IGEga2V5")
    monkeypatch.setenv("ENABLE_BANKING_SESSION_ID", SID)
    monkeypatch.setenv("ACCOUNT_LABELS", '{"hash-main": "Main"}')
    monkeypatch.setenv("LIVING_BUDGET", "1500")
    p = revolut.from_env()
    assert p is not None
    assert p.service.labels == {"hash-main": "Main"} and p.service.living_budget == 1500


def test_from_env_b64_only(monkeypatch, pem):
    monkeypatch.setenv("ENABLE_BANKING_APP_ID", APP_ID)
    monkeypatch.delenv("ENABLE_BANKING_KEY_PATH", raising=False)
    monkeypatch.setenv("ENABLE_BANKING_PRIVATE_KEY_B64", base64.b64encode(pem).decode())
    assert revolut.from_env() is not None


def test_from_env_unconfigured(monkeypatch):
    monkeypatch.setattr(
        revolut.RevolutSettings, "model_config", {**revolut.RevolutSettings.model_config, "env_file": None}
    )
    monkeypatch.delenv("ENABLE_BANKING_APP_ID", raising=False)
    assert revolut.from_env() is None


# ---------------------------------------------------------------- client: HTTP, errors, allowlist


async def test_requests_are_signed_get(client, router):
    route = router.get(f"/sessions/{SID}").mock(return_value=httpx.Response(200, json=SESSION))
    await client.session()
    req = route.calls.last.request
    assert req.method == "GET" and req.headers["authorization"].startswith("Bearer ey")
    assert "psu-ip-address" not in req.headers  # background fetch: no PSU headers


async def test_path_allowlist(client):
    with pytest.raises(EnableBankingError, match="not allowed"):
        await client._get("/sessions/some-other-session")
    with pytest.raises(EnableBankingError, match="not allowed"):
        await client._get("/aspsps")
    with pytest.raises(EnableBankingError, match="not allowed"):
        await client._get("/accounts/../payments")


async def test_missing_session_tells_you_to_link(router, wall, rsa_key):
    c = EnableBankingClient(
        signer=JwtSigner(APP_ID, rsa_key),
        session_id=None,
        transport=httpx.MockTransport(router.async_handler),
        clock=wall,
    )
    with pytest.raises(EnableBankingError, match="link_revolut.py"):
        await c.session()
    await c.aclose()


async def test_expired_session_maps_to_relink(client, router, service, caplog):
    router.get(f"/sessions/{SID}").mock(
        return_value=httpx.Response(
            401, json={"code": 401, "error": "EXPIRED_SESSION", "message": "Session is expired"}
        )
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ConsentExpiredError, match="link_revolut.py"):
        await client.session()
    from mcp.server.mcpserver.exceptions import ToolError

    client.clear_cache()
    with pytest.raises(ToolError, match="expired"):
        await _run(service.consent_status())
    assert "Bearer" not in caplog.text and "eyJ" not in caplog.text


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (401, {"error": "UNAUTHORIZED"}, "ENABLE_BANKING_APP_ID"),
        (403, {"error": "ACCESS_DENIED"}, "403 ACCESS_DENIED"),
        (500, {}, "try again later"),
        (422, {"error": "WRONG_REQUEST_PARAMETERS"}, "422 WRONG_REQUEST_PARAMETERS"),
    ],
)
async def test_error_mapping(client, router, status, body, match):
    router.get(f"/accounts/{MAIN}/balances").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(EnableBankingError, match=match):
        await client.balances(MAIN)


# ---------------------------------------------------------------- caching + PSD2 budget


async def test_balances_cached_for_ttl(client, router, wall):
    route = router.get(f"/accounts/{MAIN}/balances").mock(
        return_value=httpx.Response(200, json={"balances": [bal("ITAV", "10")]})
    )
    await client.balances(MAIN)
    wall.t += 5 * HOUR
    f = await client.balances(MAIN)
    assert route.call_count == 1 and not f.stale
    wall.t += 1.1 * HOUR
    await client.balances(MAIN)
    assert route.call_count == 2


async def test_budget_of_four_pulls_per_day_then_stale(client, router, wall):
    route = router.get(f"/accounts/{MAIN}/balances").mock(
        return_value=httpx.Response(200, json={"balances": [bal("ITAV", "10")]})
    )
    client.cache_ttl = 1.0  # force a refetch on every call
    for _ in range(4):
        await client.balances(MAIN)
        wall.t += 2
    f = await client.balances(MAIN)
    assert route.call_count == 4
    assert f.stale and "4-per-day" in f.note
    wall.t += DAY
    f = await client.balances(MAIN)
    assert route.call_count == 5 and not f.stale


async def test_429_serves_stale_and_backs_off(client, router, wall):
    route = router.get(f"/accounts/{MAIN}/balances")
    route.side_effect = [
        httpx.Response(200, json={"balances": [bal("ITAV", "10")]}),
        httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"}),
    ]
    await client.balances(MAIN)
    wall.t += 7 * HOUR
    f = await client.balances(MAIN)
    assert f.stale and "429" in f.note and f.value["balances"][0]["balance_amount"]["amount"] == "10"
    wall.t += HOUR
    f = await client.balances(MAIN)  # still inside the 6h back-off: no new request
    assert route.call_count == 2 and f.stale


async def test_429_without_cache_raises(client, router):
    router.get(f"/accounts/{MAIN}/balances").mock(
        return_value=httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
    )
    with pytest.raises(RateLimitedError, match="try again in about 6.0h"):
        await client.balances(MAIN)


async def test_transactions_paginate_with_continuation_key(client, router):
    route = router.get(f"/accounts/{MAIN}/transactions")
    route.side_effect = [
        httpx.Response(200, json={"transactions": [MAIN_TXS[0]], "continuation_key": "k1"}),
        httpx.Response(200, json={"transactions": [], "continuation_key": "k2"}),  # empty page, keep going
        httpx.Response(200, json={"transactions": [MAIN_TXS[1]], "continuation_key": None}),
    ]
    f = await client.transactions(MAIN)
    assert len(f.value) == 2 and not f.truncated
    params = [dict(c.request.url.params) for c in route.calls]
    assert params[0] == {"date_from": "2026-07-29"}
    assert params[1] == {"date_from": "2026-07-29", "continuation_key": "k1"}
    assert params[2]["continuation_key"] == "k2"


async def test_transactions_pagination_capped(client, router):
    client.max_pages = 2
    router.get(f"/accounts/{MAIN}/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": [MAIN_TXS[0]], "continuation_key": "more"})
    )
    f = await client.transactions(MAIN)
    assert f.truncated and f.meta()["truncated_history"]


async def test_transactions_window_reused_unless_older_needed(client, router):
    route = router.get(f"/accounts/{MAIN}/transactions").mock(
        return_value=httpx.Response(200, json={"transactions": []})
    )
    await client.transactions(MAIN, date(2026, 10, 1))  # inside the default 90-day window
    await client.transactions(MAIN, date(2026, 10, 20))
    assert route.call_count == 1
    await client.transactions(MAIN, date(2026, 1, 1))  # older: needs a new pull
    assert route.call_count == 2
    assert route.calls.last.request.url.params["date_from"] == "2026-01-01"
    await client.transactions(MAIN, date(2026, 10, 1))  # covered by the wider entry
    assert route.call_count == 2


# ---------------------------------------------------------------- tools


async def test_list_accounts_with_labels(service, router):
    mock_basics(router)
    out = await service.list_accounts()
    assert out["count"] == 3
    main, pocket, usd = out["accounts"]
    assert main["label"] == "Main" and main["display"] == "Main" and main["iban"] == "LT123250000000000001"
    assert pocket["display"] == "Bills pocket"
    assert "label" not in usd and usd["display"] == "USD"


async def test_get_balances_booked_and_available(service, router):
    mock_basics(router)
    router.get(f"/accounts/{MAIN}/balances").mock(
        return_value=httpx.Response(200, json={"balances": [bal("CLBD", "1200.50"), bal("ITAV", "1180.00")]})
    )
    router.get(f"/accounts/{POCKET}/balances").mock(
        return_value=httpx.Response(200, json={"balances": [bal("ITAV", "300")]})
    )
    router.get(f"/accounts/{USD}/balances").mock(
        return_value=httpx.Response(429, json={"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
    )
    out = await service.get_balances()
    main, pocket, usd = out["balances"]
    assert main["booked"] == 1200.5 and main["available"] == 1180.0 and main["booked_type"] == "CLBD"
    assert pocket["available"] == 300.0 and pocket["booked"] is None
    assert "error" in usd  # one account failing doesn't sink the rest
    assert out["total_available_by_currency"] == {"EUR": 1480.0}
    one = await service.get_balances("main")
    assert [b["account"] for b in one["balances"]] == ["Main"]


async def test_unknown_account_lists_choices(service, router):
    mock_basics(router)
    with pytest.raises(ValueError, match="Accounts: Main, Bills pocket, USD"):
        await service.get_balances("savings")


async def test_get_transactions_signed_and_paginated(service, router):
    mock_basics(router)
    out = await service.get_transactions("Main", from_date="2026-10-01", limit=3)
    assert out["total_count"] == 12  # the September row is outside the range
    first = out["transactions"][0]
    assert first["date"] == "2026-10-26" and first["amount"] < 0 and first["account"] == "Main"
    assert out["next_cursor"] == "3"
    salary = (await service.get_transactions("Main", from_date="2026-10-01", to_date="2026-10-01"))["transactions"]
    assert salary == [salary[0]] and salary[0]["amount"] == 2500.0 and salary[0]["counterparty"] == "ACME SL"
    last = await service.get_transactions("Main", from_date="2026-10-01", limit=5, cursor="10")
    assert last["returned"] == 2 and last["next_cursor"] is None
    with pytest.raises(ValueError, match="cursor"):
        await service.get_transactions(cursor="abc")


async def test_spending_summary_month(service, router):
    mock_basics(router)
    out = await service.spending_summary("month")
    assert (out["from_date"], out["to_date"]) == ("2026-10-01", "2026-10-27")
    (eur,) = out["summaries"]
    cats = {c["category"]: c["amount"] for c in eur["by_category"]}
    assert cats == {
        "groceries": 42.1,
        "eating_out": 12.5,
        "transport": 8.0,
        "subscriptions": 15.99,
        "shopping": 89.98,
    }
    assert eur["total_out"] == round(sum(cats.values()), 2)
    assert eur["total_in"] == 2500.0
    assert eur["excluded"]["internal_transfers"] == {"count": 2, "out": 400.0, "in": 0}  # pocket + own name
    assert eur["excluded"]["trading212"]["out"] == 500.0
    top = {m["merchant"]: m for m in eur["top_merchants"]}
    assert top["AMZN Mktp ES"]["count"] == 2 and top["AMZN Mktp ES"]["amount"] == 29.98
    assert "Rejected Cafe" not in top and "Old Restaurant" not in top


async def test_spending_summary_periods(service, router):
    mock_basics(router)
    assert (await service.spending_summary("today"))["summaries"] == []
    y = await service.spending_summary("yesterday")
    assert y["from_date"] == "2026-10-26" and y["summaries"][0]["total_out"] == 54.6
    w = await service.spending_summary("week")
    assert w["from_date"] == "2026-10-26"  # Monday
    c = await service.spending_summary("custom", from_date="2026-09-01", to_date="2026-09-30")
    assert c["summaries"][0]["by_category"] == [{"category": "eating_out", "amount": 30.0, "count": 1, "pct": 100.0}]
    with pytest.raises(ValueError, match="needs from_date"):
        await service.spending_summary("custom")


async def test_spending_summary_separates_currencies(service, router):
    mock_basics(
        router, {MAIN: MAIN_TXS[:1], POCKET: [], USD: [tx("20", "DBIT", "2026-10-26", creditor="Starbucks", ccy="USD")]}
    )
    out = await service.spending_summary("month")
    assert [s["currency"] for s in out["summaries"]] == ["EUR", "USD"]


async def test_consent_status(service, router, wall):
    router.get(f"/sessions/{SID}").mock(return_value=httpx.Response(200, json=SESSION))
    out = await service.consent_status()
    assert out["days_left"] == 174 and out["status"] == "AUTHORIZED" and "warning" not in out
    service.client.clear_cache()
    wall.t = datetime(2027, 4, 10, tzinfo=UTC).timestamp()
    out = await service.consent_status()
    assert out["days_left"] == 10 and "Re-link soon" in out["warning"] and "link_revolut.py" in out["how_to_relink"]


POCKET_TXS = [  # shapes taken from real Revolut pocket data
    tx("100.00", "DBIT", "2026-10-20", creditor="Mercadona"),
    tx("4.50", "DBIT", "2026-10-21", creditor="Torre Ie"),
    tx("300.00", "CRDT", "2026-10-01", info=["To EUR Bills"]),  # top-up from main account
    tx("5.00", "CRDT", "2026-10-23", info=["Your balance was still negative, so we moved money from Free Spend"]),
    tx("25.00", "DBIT", "2026-10-24", info=["To EUR MB:8f3a"]),  # move to another pocket
    tx("10.00", "CRDT", "2026-10-22", debtor="Amazon"),  # refund
    tx("7.00", "DBIT", "2026-10-26", creditor="Rejected", status="RJCT"),
    tx("50.00", "DBIT", "2026-09-28", creditor="Mercadona"),  # last month
]


async def test_budget_status_tracks_living_pocket(service, router):
    mock_basics(router, {MAIN: MAIN_TXS, POCKET: POCKET_TXS, USD: []})
    service.living_account = "Bills pocket"
    out = await service.budget_status()
    assert out["account"] == "Bills pocket" and out["month"] == "2026-10"
    assert (out["gross_spend"], out["refunds"], out["spent"]) == (104.5, 10.0, 94.5)
    assert out["budget"] == 1500.0 and out["remaining"] == 1405.5
    assert out["pct_month_elapsed"] == 87.1  # 27 of 31 days
    assert out["pct_budget_used"] == 6.3
    assert out["projected_month_end"] == 108.5  # 94.5 / 27 * 31
    assert out["days_left"] == 4 and out["daily_allowance_rest_of_month"] == 351.38
    assert out["status"] == "on_track"
    assert {c["category"] for c in out["by_category"]} == {"groceries", "eating_out"}
    # Only the living pocket is read; main-account spending does not count.
    assert all(s["account"] == "Bills pocket" for s in out["sources"])


@pytest.mark.parametrize(("budget", "status"), [(100.0, "over_pace"), (50.0, "over_budget")])
async def test_budget_status_flags_pace(service, router, budget, status):
    mock_basics(router, {MAIN: [], POCKET: POCKET_TXS, USD: []})
    service.living_account, service.living_budget = "Bills", budget
    out = await service.budget_status()
    assert out["status"] == status
    if status == "over_budget":
        assert out["remaining"] < 0 and out["daily_allowance_rest_of_month"] is None


async def test_budget_status_unknown_account(service, router):
    mock_basics(router)
    service.living_account = "Nope"
    with pytest.raises(ValueError, match="LIVING_ACCOUNT"):
        await service.budget_status()


# ---------------------------------------------------------------- categorization rules


@pytest.mark.parametrize(
    ("text", "mcc", "expected"),
    [
        ("UBER *EATS", None, "eating_out"),
        ("Uber BV", None, "transport"),
        ("Amazon Prime", None, "subscriptions"),
        ("Amazon.es", None, "shopping"),
        ("Ryanair", None, "travel"),
        ("Lidl", None, "groceries"),
        ("Card transaction", None, "other"),  # 'action' must not match 'transaction'
        ("Salsa club", None, "other"),  # 'alsa' must not match 'salsa'
        ("Random", "5411", "groceries"),
        ("Mercadona", "bogus", "groceries"),
        ("TORRE IE", None, "eating_out"),
        ("Plan fee - Ultra", None, "subscriptions"),
    ],
)
def test_category_rules(text, mcc, expected):
    assert cat.category_for(text, mcc) == expected


def test_internal_detection_is_conservative():
    own = ["food", "lt12 3250 0000 0000 0001", "jad test"]
    assert cat.is_internal("To pocket EUR Holiday", own)
    assert cat.is_internal("Transfer to LT123250000000000001", own)
    assert cat.is_internal("Jad Test", own)
    assert cat.is_internal("Transfer to food", own)
    assert not cat.is_internal("Whole Foods Market", own)
    assert cat.is_internal("To EUR MB:8f3a", [])
    assert cat.is_internal("To EUR", [])
    assert cat.is_internal("From USD Savings", [])
    assert cat.is_internal("Your balance was still negative, so we moved money", [])
    assert not cat.is_internal("Europcar", [])
    assert cat.is_trading212("TRADING 212 MARKETS")
    assert not cat.is_trading212("Grocery")
