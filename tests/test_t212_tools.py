from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer

from finance_mcp.providers.trading212 import analytics
from finance_mcp.providers.trading212.client import Trading212Error
from finance_mcp.providers.trading212.tools import register_tools
from tests.conftest import (
    EXCHANGES,
    INSTRUMENTS,
    POSITIONS,
    SUMMARY,
    dividend,
    order,
    tx,
)

P = "/api/v0/equity"


def paged(path: str, pages: list[list[dict]]):
    """side_effect serving `pages` in order via cursor=index."""

    def handler(request: httpx.Request) -> httpx.Response:
        idx = int(request.url.params.get("cursor", 0))
        nxt = f"{path}?limit=50&cursor={idx + 1}" if idx + 1 < len(pages) else None
        return httpx.Response(200, json={"items": pages[idx], "nextPagePath": nxt})

    return handler


@pytest.fixture
def mock_basics(router):
    router.get(f"{P}/account/summary").respond(json=SUMMARY)
    router.get(f"{P}/positions").mock(
        side_effect=lambda req: httpx.Response(
            200,
            json=[p for p in POSITIONS if req.url.params.get("ticker") in (None, p["instrument"]["ticker"])],
        )
    )
    router.get(f"{P}/metadata/instruments").respond(json=INSTRUMENTS)
    router.get(f"{P}/metadata/exchanges").respond(json=EXCHANGES)
    return router


# ---------------------------------------------------------------- raw tools


async def test_get_account_summary(service, mock_basics):
    out = await service.get_account_summary()
    assert out["account_currency"] == "GBP"
    assert out["total_value"] == 12000.0
    assert out["investments"]["unrealized_pnl_pct"] == pytest.approx(21.67)
    assert out["cash"]["available_to_trade"] == 1000.0


async def test_get_positions_compact_and_sorted(service, mock_basics):
    out = await service.get_positions()
    assert out["count"] == 3
    assert [p["ticker"] for p in out["positions"]] == ["AAPL_US_EQ", "VUSAl_EQ", "SAP_DE_EQ"]
    aapl = out["positions"][0]
    assert aapl["weight_pct"] == pytest.approx(54.79, abs=0.01)
    assert aapl["unrealized_pnl_pct"] == 50.0
    assert "walletImpact" not in aapl
    by_pnl = await service.get_positions(sort_by="pnl_pct")
    assert by_pnl["positions"][-1]["ticker"] == "VUSAl_EQ"


async def test_get_positions_raw(service, mock_basics):
    out = await service.get_positions(raw=True)
    assert out["positions"][0]["walletImpact"]["currentValue"] == 6000.0


async def test_get_position_by_full_ticker_symbol_and_isin(service, mock_basics):
    assert (await service.get_position("AAPL_US_EQ"))["positions"][0]["ticker"] == "AAPL_US_EQ"
    assert (await service.get_position("sap"))["positions"][0]["ticker"] == "SAP_DE_EQ"
    assert (await service.get_position("ISINAAPL"))["positions"][0]["ticker"] == "AAPL_US_EQ"
    with pytest.raises(Trading212Error, match="search_instruments"):
        await service.get_position("TSLA")


async def test_get_order_history_filters_and_stops_early(service, router):
    pages = [
        [
            order(5, "AAPL_US_EQ", "BUY", "2026-09-01T10:00:00Z", 2, 200),
            order(4, "SAP_DE_EQ", "SELL", "2026-06-01T10:00:00Z", 1, 150),
        ],
        [order(3, "AAPL_US_EQ", "BUY", "2025-12-01T10:00:00Z", 1, 180)],
        [order(2, "AAPL_US_EQ", "BUY", "2024-01-01T10:00:00Z", 1, 100)],
    ]
    route = router.get(f"{P}/history/orders").mock(side_effect=paged(f"{P}/history/orders", pages))
    out = await service.get_order_history("2026-01-01", None, None, 50, False)
    assert [o["order_id"] for o in out["orders"]] == [5, 4]
    assert route.call_count == 2  # page 2 is entirely older than from_date -> stop
    first = out["orders"][0]
    assert first["side"] == "BUY" and first["fees_and_taxes"] == 0.15 and first["net_value"] == 320.0


async def test_get_order_history_passes_ticker_and_limit(service, router):
    route = router.get(f"{P}/history/orders").respond(json={"items": [], "nextPagePath": None})
    out = await service.get_order_history(None, None, "aapl_us_eq", 10, False)
    assert route.calls.last.request.url.params["ticker"] == "AAPL_US_EQ"
    assert out["count"] == 0


async def test_get_order_history_bad_date(service):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await service.get_order_history("01/02/2026", None, None, 50, False)


async def test_get_dividends_with_range_and_total(service, router):
    items = [
        dividend("AAPL_US_EQ", 5.0, "2026-08-15T00:00:00Z"),
        dividend("VUSAl_EQ", 12.0, "2026-07-01T00:00:00Z"),
        dividend("AAPL_US_EQ", 4.0, "2026-05-15T00:00:00Z"),
    ]
    router.get(f"{P}/history/dividends").respond(json={"items": items, "nextPagePath": None})
    out = await service.get_dividends("2026-06-01", "2026-08-31", None, 100, False)
    assert out["count"] == 2 and out["total"] == 17.0
    assert out["dividends"][0] == {
        "paid_on": "2026-08-15",
        "ticker": "AAPL_US_EQ",
        "name": "AAPL",
        "amount": 5.0,
        "currency": "GBP",
        "quantity": 10,
        "gross_per_share": 0.5,
        "type": "ORDINARY",
    }


async def test_get_transactions_type_filter_and_totals(service, router):
    items = [
        tx("DEPOSIT", 500, "2026-03-01T00:00:00Z"),
        tx("FEE", -1.5, "2026-02-01T00:00:00Z"),
        tx("DEPOSIT", 1000, "2026-01-01T00:00:00Z"),
    ]
    router.get(f"{P}/history/transactions").respond(json={"items": items, "nextPagePath": None})
    out = await service.get_transactions(None, None, "DEPOSIT", 100, False)
    assert out["count"] == 2 and out["totals_by_type"] == {"DEPOSIT:GBP": 1500.0}
    assert out["transactions"][0]["amount"] == 500


async def test_get_pies_and_pie(service, router):
    router.get(f"{P}/pies").respond(
        json=[
            {
                "id": 7,
                "cash": 3.2,
                "progress": 0.4,
                "status": "AHEAD",
                "result": {
                    "priceAvgInvestedValue": 1000,
                    "priceAvgValue": 1100,
                    "priceAvgResult": 100,
                    "priceAvgResultCoef": 0.1,
                },
                "dividendDetails": {"gained": 12, "inCash": 2, "reinvested": 10},
            }
        ]
    )
    router.get(f"{P}/pies/7").respond(
        json={
            "settings": {
                "id": 7,
                "name": "Core",
                "goal": 5000,
                "dividendCashAction": "REINVEST",
                "creationDate": "2024-01-01T00:00:00Z",
            },
            "instruments": [
                {
                    "ticker": "VUSAl_EQ",
                    "expectedShare": 0.6,
                    "currentShare": 0.62,
                    "ownedQuantity": 3,
                    "result": {"priceAvgValue": 680},
                    "issues": [],
                }
            ],
        }
    )
    pies = await service.get_pies()
    assert pies["pies"][0] == {
        "id": 7,
        "invested": 1000,
        "value": 1100,
        "result": 100,
        "result_pct": 10.0,
        "uninvested_cash": 3.2,
        "dividends_gained": 12,
        "goal_progress_pct": 40.0,
        "status": "AHEAD",
    }
    pie = await service.get_pie(7)
    assert pie["name"] == "Core"
    assert pie["instruments"][0]["target_pct"] == 60.0 and pie["instruments"][0]["current_pct"] == 62.0


async def test_search_instruments_ranks_matches(service, mock_basics):
    out = await service.search_instruments("aapl")
    assert out["results"][0]["ticker"] == "AAPL_US_EQ"
    assert out["results"][0]["exchange"] == "NASDAQ"
    assert (await service.search_instruments("US0378331005"))["results"][0]["ticker"] == "AAPL_US_EQ"
    assert (await service.search_instruments("vanguard"))["results"][0]["ticker"] == "VUSAl_EQ"
    etfs = await service.search_instruments("a", type_="ETF")
    assert {r["type"] for r in etfs["results"]} == {"ETF"}


async def test_search_instruments_uses_24h_cache(service, mock_basics, clock):
    await service.search_instruments("aapl")
    clock.t += 3600
    await service.search_instruments("sap")
    assert mock_basics.routes[2].call_count == 1  # instruments route


# ---------------------------------------------------------------- analytics


async def test_portfolio_overview(service, mock_basics):
    out = await service.portfolio_overview(top_n=2)
    t = out["totals"]
    assert t["positions_value"] == 10950.0 and t["cost_basis"] == 9000.0 and t["unrealized_pnl"] == 1950.0
    by_type = {x["key"]: x["weight_pct"] for x in out["allocation"]["by_type"]}
    assert by_type == {"STOCK": pytest.approx(63.47, abs=0.01), "ETF": pytest.approx(36.53, abs=0.01)}
    by_ex = {x["key"] for x in out["allocation"]["by_exchange"]}
    assert by_ex == {"NASDAQ", "London Stock Exchange", "XETRA"}
    assert {x["key"] for x in out["allocation"]["by_currency"]} == {"USD", "GBP", "EUR"}
    assert [x["ticker"] for x in out["top_performers"]] == ["AAPL_US_EQ", "SAP_DE_EQ"]
    assert out["bottom_performers"][0]["ticker"] == "VUSAl_EQ"
    assert set(out["positions"][0]) <= {"ticker", "value", "weight_pct", "unrealized_pnl_pct"}
    assert len(json.dumps(out)) < 4000, "overview should stay compact"


async def test_portfolio_overview_degrades_without_metadata_scope(service, router):
    router.get(f"{P}/account/summary").respond(json=SUMMARY)
    router.get(f"{P}/positions").respond(json=POSITIONS)
    router.get(f"{P}/metadata/instruments").respond(403)
    out = await service.portfolio_overview()
    assert out["allocation"]["by_type"][0]["key"] == "UNKNOWN"
    assert "metadata" in out["warnings"][0]


async def test_dividend_report_ttm(service, router):
    items = [
        dividend("AAPL_US_EQ", 5.0, "2026-10-01T00:00:00Z"),
        dividend("VUSAl_EQ", 12.0, "2026-07-01T00:00:00Z"),
        dividend("AAPL_US_EQ", 4.0, "2026-04-01T00:00:00Z"),
        dividend("AAPL_US_EQ", 3.0, "2025-01-15T00:00:00Z"),  # outside TTM
    ]
    router.get(f"{P}/history/dividends").respond(json={"items": items, "nextPagePath": None})
    out = await service.dividend_report("ttm")
    assert out["total"] == 21.0 and out["payments"] == 3
    assert out["trailing_12m_income"] == 21.0 and out["trailing_12m_monthly_avg"] == 1.75
    months = {m["month"]: m["total"] for m in out["by_month"]}
    assert len(months) == 13 and months["2026-10"] == 5.0 and months["2026-09"] == 0.0
    assert out["by_ticker"][0] == {
        "ticker": "VUSAl_EQ",
        "name": "VUSAl",
        "total": 12.0,
        "count": 1,
        "share_pct": pytest.approx(57.14, abs=0.01),
    }


async def test_dividend_report_year_and_validation(service, router):
    items = [dividend("AAPL_US_EQ", 3.0, "2025-01-15T00:00:00Z"), dividend("AAPL_US_EQ", 2.0, "2024-12-15T00:00:00Z")]
    router.get(f"{P}/history/dividends").respond(json={"items": items, "nextPagePath": None})
    out = await service.dividend_report("2025")
    assert out["total"] == 3.0 and len(out["by_month"]) == 12
    with pytest.raises(ValueError):
        await service.dividend_report("lastweek")


async def test_net_deposits_and_returns(service, router):
    router.get(f"{P}/account/summary").respond(json=SUMMARY)  # totalValue 12000
    items = [
        tx("WITHDRAW", -1000, "2026-01-01T00:00:00Z"),
        tx("FEE", -2, "2025-06-01T00:00:00Z"),
        tx("INTEREST_ON_FREE_CASH", 3, "2025-05-01T00:00:00Z"),
        tx("DEPOSIT", 5000, "2025-01-01T00:00:00Z"),
        tx("DEPOSIT", 100, "2025-01-01T00:00:00Z", ccy="EUR"),
        tx("DEPOSIT", 6000, "2024-10-27T00:00:00Z"),
    ]
    router.get(f"{P}/history/transactions").respond(json={"items": items, "nextPagePath": None})
    out = await service.net_deposits_and_returns()
    assert out["deposits"] == 11000 and out["withdrawals"] == 1000 and out["net_deposits"] == 10000
    assert out["total_gain"] == 2000 and out["simple_return_pct"] == 20.0
    assert out["fees_paid"] == 2 and out["interest_received"] == 3
    assert 5 < out["money_weighted_return_annualized_pct"] < 20
    assert any("EUR" in w for w in out["warnings"])


def test_xirr_matches_known_value():
    flows = [(datetime(2025, 1, 1, tzinfo=UTC), -1000.0), (datetime(2026, 1, 1, tzinfo=UTC), 1100.0)]
    assert analytics.xirr(flows) == pytest.approx(0.0998, abs=0.001)
    assert analytics.xirr([(datetime(2025, 1, 1, tzinfo=UTC), -1000.0)]) is None


# ---------------------------------------------------------------- MCP registration


EXPECTED_TOOLS = {
    "get_account_summary",
    "get_positions",
    "get_position",
    "get_order_history",
    "get_dividends",
    "get_transactions",
    "get_pies",
    "get_pie",
    "search_instruments",
    "portfolio_overview",
    "dividend_report",
    "net_deposits_and_returns",
}


async def test_tools_registered_read_only(service):
    mcp = MCPServer("test")
    register_tools(mcp, service)
    tools = await mcp.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False
        assert "Trading 212" in t.description


async def test_tool_errors_reach_model_cleanly(service, router):
    router.get(f"{P}/account/summary").respond(401)
    mcp = MCPServer("test")
    register_tools(mcp, service)
    async with Client(mcp) as c:
        result = await c.call_tool("get_account_summary", {})
    assert result.is_error
    assert "rejected the API credentials" in result.content[0].text


async def test_tool_call_returns_json_text(service, mock_basics):
    mcp = MCPServer("test")
    register_tools(mcp, service)
    async with Client(mcp) as c:
        result = await c.call_tool("portfolio_overview", {"top_n": 1})
    assert not result.is_error
    data = json.loads(result.content[0].text)
    assert data["positions_count"] == 3
