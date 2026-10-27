from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from finance_mcp.providers.trading212.client import Trading212Client
from finance_mcp.providers.trading212.tools import Trading212Service

BASE = "https://demo.trading212.com"
API_KEY = "KEY_abc123"
API_SECRET = "SECRET_xyz789"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


class FakeClock:
    """Deterministic monotonic + wall clock; sleep advances time instantly."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def wall(self) -> float:
        return 1_800_000_000.0 + self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def router() -> respx.Router:
    return respx.Router(base_url=BASE, assert_all_mocked=True, assert_all_called=False)


@pytest.fixture
async def client(router: respx.Router, clock: FakeClock):
    c = Trading212Client(
        env="demo",
        api_key=API_KEY,
        api_secret=API_SECRET,
        transport=httpx.MockTransport(router.async_handler),
        clock=clock,
        wall_clock=clock.wall,
        sleep=clock.sleep,
    )
    yield c
    await c.aclose()


@pytest.fixture
def service(client: Trading212Client) -> Trading212Service:
    return Trading212Service(client, now=lambda: NOW)


# ---------------------------------------------------------------- sample data

SUMMARY = {
    "id": 123456,
    "currency": "GBP",
    "totalValue": 12000.0,
    "cash": {"availableToTrade": 1000.0, "inPies": 50.0, "reservedForOrders": 0.0},
    "investments": {
        "currentValue": 10950.0,
        "totalCost": 9000.0,
        "unrealizedProfitLoss": 1950.0,
        "realizedProfitLoss": 300.0,
    },
}


def position(ticker: str, name: str, ccy: str, value: float, cost: float, qty: float = 10.0) -> dict:
    return {
        "instrument": {"ticker": ticker, "name": name, "isin": f"ISIN{ticker[:4]}", "currency": ccy},
        "quantity": qty,
        "quantityAvailableForTrading": qty,
        "quantityInPies": 0,
        "averagePricePaid": cost / qty,
        "currentPrice": value / qty,
        "createdAt": "2024-01-02T10:00:00Z",
        "walletImpact": {
            "currency": "GBP",
            "currentValue": value,
            "totalCost": cost,
            "unrealizedProfitLoss": value - cost,
            "fxImpact": None,
        },
    }


POSITIONS = [
    position("AAPL_US_EQ", "Apple Inc", "USD", 6000.0, 4000.0),
    position("VUSAl_EQ", "Vanguard S&P 500", "GBP", 4000.0, 4200.0),
    position("SAP_DE_EQ", "SAP SE", "EUR", 950.0, 800.0),
]

INSTRUMENTS = [
    {"ticker": "AAPL_US_EQ", "name": "Apple Inc", "shortName": "AAPL", "isin": "US0378331005",
     "currencyCode": "USD", "type": "STOCK", "workingScheduleId": 1},
    {"ticker": "VUSAl_EQ", "name": "Vanguard S&P 500 UCITS ETF", "shortName": "VUSA", "isin": "IE00B3XXRP09",
     "currencyCode": "GBP", "type": "ETF", "workingScheduleId": 2},
    {"ticker": "SAP_DE_EQ", "name": "SAP SE", "shortName": "SAP", "isin": "DE0007164600",
     "currencyCode": "EUR", "type": "STOCK", "workingScheduleId": 3},
    {"ticker": "APP_US_EQ", "name": "AppLovin", "shortName": "APP", "isin": "US03831W1080",
     "currencyCode": "USD", "type": "STOCK", "workingScheduleId": 1},
]

EXCHANGES = [
    {"id": 10, "name": "NASDAQ", "workingSchedules": [{"id": 1, "timeEvents": []}]},
    {"id": 11, "name": "London Stock Exchange", "workingSchedules": [{"id": 2, "timeEvents": []}]},
    {"id": 12, "name": "XETRA", "workingSchedules": [{"id": 3, "timeEvents": []}]},
]


def dividend(ticker: str, amount: float, paid_on: str) -> dict:
    return {
        "ticker": ticker,
        "instrument": {"ticker": ticker, "name": ticker.split("_")[0]},
        "amount": amount,
        "currency": "GBP",
        "paidOn": paid_on,
        "quantity": 10,
        "grossAmountPerShare": amount / 10,
        "type": "ORDINARY",
        "reference": f"ref-{ticker}-{paid_on}",
    }


def tx(type_: str, amount: float, when: str, ccy: str = "GBP") -> dict:
    return {"type": type_, "amount": amount, "currency": ccy, "dateTime": when, "reference": f"{type_}-{when}"}


def order(oid: int, ticker: str, side: str, when: str, qty: float, price: float) -> dict:
    return {
        "order": {
            "id": oid, "ticker": ticker, "side": side, "type": "MARKET", "status": "FILLED",
            "createdAt": when, "currency": "USD", "quantity": qty, "filledQuantity": qty,
        },
        "fill": {
            "id": oid * 10, "filledAt": when, "price": price, "quantity": qty, "type": "TRADE",
            "walletImpact": {
                "currency": "GBP", "fxRate": 0.8, "netValue": qty * price * 0.8, "realisedProfitLoss": 0,
                "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "quantity": 0.15, "currency": "GBP"}],
            },
        },
    }
