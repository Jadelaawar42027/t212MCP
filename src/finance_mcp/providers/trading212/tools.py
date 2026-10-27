"""Trading 212 MCP tools: raw read tools plus server-side analytics.

`Trading212Service` holds the logic (testable without MCP). `register_tools`
exposes it on an MCPServer with read-only annotations.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from finance_mcp.providers.trading212 import analytics as a
from finance_mcp.providers.trading212.client import Trading212Client, Trading212Error
from finance_mcp.providers.trading212.models import (
    AccountSummary,
    Dividend,
    Exchange,
    HistoricalOrder,
    Pie,
    PieDetail,
    Position,
    TradableInstrument,
    Transaction,
)

P = "/api/v0/equity"
PATH_SUMMARY = f"{P}/account/summary"
PATH_POSITIONS = f"{P}/positions"
PATH_ORDERS = f"{P}/history/orders"
PATH_DIVIDENDS = f"{P}/history/dividends"
PATH_TRANSACTIONS = f"{P}/history/transactions"
PATH_INSTRUMENTS = f"{P}/metadata/instruments"
PATH_EXCHANGES = f"{P}/metadata/exchanges"
PATH_PIES = f"{P}/pies"

T = TypeVar("T")


def _iso(dt: datetime | None) -> str | None:
    return a.aware(dt).isoformat() if dt else None


def _day(dt: datetime | None) -> str | None:
    return a.aware(dt).date().isoformat() if dt else None


def _dump(model: Any) -> dict:
    return model.model_dump(mode="json", exclude_none=True)


def _parse(model: type[T], data: Any) -> T:
    try:
        return model.model_validate(data or {})  # type: ignore[attr-defined]
    except ValidationError:
        raise Trading212Error(f"Unexpected response shape from Trading 212 ({model.__name__}).") from None


def _parse_list(model: type[T], data: Any) -> list[T]:
    if not isinstance(data, list):
        raise Trading212Error(f"Unexpected response shape from Trading 212 ({model.__name__} list).")
    return [_parse(model, item) for item in data]


class Trading212Service:
    def __init__(self, client: Trading212Client, *, now: Callable[[], datetime] | None = None) -> None:
        self.client = client
        self._now = now or (lambda: datetime.now(UTC))

    # ---------- data access ----------

    async def _summary(self) -> AccountSummary:
        return _parse(AccountSummary, await self.client.get(PATH_SUMMARY))

    async def _positions(self, ticker: str | None = None) -> list[Position]:
        return _parse_list(Position, await self.client.get(PATH_POSITIONS, {"ticker": ticker}))

    async def _instruments(self) -> list[TradableInstrument]:
        return _parse_list(TradableInstrument, await self.client.get(PATH_INSTRUMENTS))

    async def _exchange_by_schedule(self) -> dict[int, str]:
        exchanges = _parse_list(Exchange, await self.client.get(PATH_EXCHANGES))
        out: dict[int, str] = {}
        for ex in exchanges:
            for ws in ex.workingSchedules:
                if ws.id is not None:
                    out[ws.id] = ex.name or f"exchange {ex.id}"
        return out

    async def _instrument_meta(self) -> tuple[dict[str, dict], list[str]]:
        """ticker -> {type, exchange, name, short_name}. Degrades gracefully on failure."""
        warnings: list[str] = []
        try:
            instruments = await self._instruments()
        except Trading212Error as exc:
            return {}, [f"Instrument metadata unavailable: {exc}"]
        try:
            schedules = await self._exchange_by_schedule()
        except Trading212Error as exc:
            schedules, warnings = {}, [f"Exchange metadata unavailable: {exc}"]
        meta = {
            i.ticker: {
                "type": i.type or "UNKNOWN",
                "exchange": schedules.get(i.workingScheduleId or -1, "UNKNOWN"),
                "name": i.name,
                "short_name": i.shortName,
                "currency": i.currencyCode,
                "isin": i.isin,
            }
            for i in instruments
            if i.ticker
        }
        return meta, warnings

    async def _history(
        self,
        path: str,
        model: type[T],
        date_of: Callable[[T], datetime | None],
        *,
        start: datetime | None,
        end: datetime | None,
        ticker: str | None = None,
    ) -> tuple[list[T], bool]:
        """Fetch history newest-first, stopping once a whole page is older than `start`.
        The API has no date filters, so filtering happens here."""

        def older_than_start(page: list[Any]) -> bool:
            if start is None:
                return False
            dates = [a.aware(date_of(_parse(model, item))) for item in page]
            return all(d is not None and d < start for d in dates)

        page = await self.client.get_all_pages(path, {"ticker": ticker}, stop=older_than_start)
        items = [_parse(model, raw) for raw in page.items]
        return [i for i in items if a.in_range(date_of(i), start, end)], page.truncated

    # ---------- formatting ----------

    @staticmethod
    def _position_row(p: Position, total: float | None, meta: dict[str, dict] | None = None) -> dict:
        w = p.walletImpact
        row = {
            "ticker": p.ticker,
            "name": p.instrument.name,
            "quantity": p.quantity,
            "avg_price": a.r(p.averagePricePaid, 4),
            "current_price": a.r(p.currentPrice, 4),
            "instrument_currency": p.instrument.currency,
            "value": a.r(w.currentValue),
            "cost": a.r(w.totalCost),
            "unrealized_pnl": a.r(w.unrealizedProfitLoss),
            "unrealized_pnl_pct": a.pct(w.unrealizedProfitLoss, w.totalCost),
            "fx_impact": a.r(w.fxImpact),
            "weight_pct": a.pct(w.currentValue, total),
        }
        if p.quantityInPies:
            row["quantity_in_pies"] = p.quantityInPies
        if meta and p.ticker in meta:
            row["type"] = meta[p.ticker]["type"]
            row["exchange"] = meta[p.ticker]["exchange"]
        return {k: v for k, v in row.items() if v is not None}

    @staticmethod
    def _order_row(h: HistoricalOrder) -> dict:
        o, f = h.order, h.fill
        wi = f.walletImpact if f else None
        fees = sum(t.quantity or 0 for t in (wi.taxes if wi else []))
        row = {
            "date": _iso(h.when),
            "ticker": o.ticker or (o.instrument.ticker if o.instrument else None),
            "side": o.side,
            "type": o.type,
            "status": o.status,
            "quantity": o.filledQuantity if o.filledQuantity is not None else o.quantity,
            "fill_price": f.price if f else None,
            "instrument_currency": o.currency,
            "net_value": a.r(wi.netValue) if wi else None,
            "wallet_currency": wi.currency if wi else None,
            "realized_pnl": a.r(wi.realisedProfitLoss) if wi else None,
            "fees_and_taxes": a.r(fees) if fees else None,
            "order_id": o.id,
        }
        return {k: v for k, v in row.items() if v is not None}

    @staticmethod
    def _dividend_row(d: Dividend) -> dict:
        row = {
            "paid_on": _day(d.paidOn),
            "ticker": d.ticker,
            "name": d.instrument.name if d.instrument else None,
            "amount": a.r(d.amount),
            "currency": d.currency,
            "quantity": d.quantity,
            "gross_per_share": d.grossAmountPerShare,
            "type": d.type,
        }
        return {k: v for k, v in row.items() if v is not None}

    @staticmethod
    def _transaction_row(t: Transaction) -> dict:
        row = {
            "date": _iso(t.dateTime),
            "type": t.type,
            "amount": a.r(t.amount),
            "currency": t.currency,
            "reference": t.reference,
        }
        return {k: v for k, v in row.items() if v is not None}

    # ---------- raw tools ----------

    async def get_account_summary(self) -> dict:
        s = await self._summary()
        inv, cash = s.investments, s.cash
        return {
            "account_currency": s.currency,
            "total_value": a.r(s.totalValue),
            "cash": {
                "available_to_trade": a.r(cash.availableToTrade),
                "reserved_for_orders": a.r(cash.reservedForOrders),
                "in_pies_uninvested": a.r(cash.inPies),
            },
            "investments": {
                "current_value": a.r(inv.currentValue),
                "cost_basis": a.r(inv.totalCost),
                "unrealized_pnl": a.r(inv.unrealizedProfitLoss),
                "unrealized_pnl_pct": a.pct(inv.unrealizedProfitLoss, inv.totalCost),
                "realized_pnl_all_time": a.r(inv.realizedProfitLoss),
            },
            "env": self.client.env,
        }

    async def get_positions(self, sort_by: str = "value", raw: bool = False) -> dict:
        positions = await self._positions()
        if raw:
            return {"count": len(positions), "positions": [_dump(p) for p in positions]}
        total = sum(p.walletImpact.currentValue or 0 for p in positions)
        rows = [self._position_row(p, total) for p in positions]
        keys = {
            "value": lambda r: -(r.get("value") or 0),
            "pnl": lambda r: -(r.get("unrealized_pnl") or 0),
            "pnl_pct": lambda r: -(r.get("unrealized_pnl_pct") or 0),
            "ticker": lambda r: r.get("ticker") or "",
        }
        rows.sort(key=keys.get(sort_by, keys["value"]))
        currency = next((p.walletImpact.currency for p in positions if p.walletImpact.currency), None)
        return {"count": len(rows), "total_value": a.r(total), "currency": currency, "positions": rows}

    async def get_position(self, ticker: str, raw: bool = False) -> dict:
        q = ticker.strip().upper()
        if not q:
            raise ValueError("ticker is required")
        matches = await self._positions(q) if "_" in q else []
        if not matches:
            all_positions = await self._positions()
            matches = [
                p
                for p in all_positions
                if p.ticker.upper() == q
                or p.ticker.upper().split("_")[0] == q
                or (p.instrument.isin or "").upper() == q
            ]
        if not matches:
            raise Trading212Error(
                f"No open position matching '{ticker}'. Use search_instruments to find the Trading 212 "
                "ticker (e.g. AAPL_US_EQ), or get_positions to list holdings."
            )
        if raw:
            return {"positions": [_dump(p) for p in matches]}
        return {"positions": [self._position_row(p, None) for p in matches]}

    async def get_order_history(
        self, from_date: str | None, to_date: str | None, ticker: str | None, limit: int, raw: bool
    ) -> dict:
        start, end = a.parse_day(from_date), a.parse_day(to_date, end=True)
        orders, truncated = await self._history(
            PATH_ORDERS, HistoricalOrder, lambda h: h.when, start=start, end=end, ticker=_t(ticker)
        )
        shown = orders[:limit]
        return {
            "count": len(orders),
            "returned": len(shown),
            "truncated_history": truncated,
            "orders": [_dump(o) for o in shown] if raw else [self._order_row(o) for o in shown],
        }

    async def get_dividends(
        self, from_date: str | None, to_date: str | None, ticker: str | None, limit: int, raw: bool
    ) -> dict:
        start, end = a.parse_day(from_date), a.parse_day(to_date, end=True)
        divs, truncated = await self._history(
            PATH_DIVIDENDS, Dividend, lambda d: d.paidOn, start=start, end=end, ticker=_t(ticker)
        )
        shown = divs[:limit]
        return {
            "count": len(divs),
            "total": a.r(sum(d.amount or 0 for d in divs)),
            "currency": next((d.currency for d in divs if d.currency), None),
            "returned": len(shown),
            "truncated_history": truncated,
            "dividends": [_dump(d) for d in shown] if raw else [self._dividend_row(d) for d in shown],
        }

    async def get_transactions(
        self, from_date: str | None, to_date: str | None, type_: str | None, limit: int, raw: bool
    ) -> dict:
        start, end = a.parse_day(from_date), a.parse_day(to_date, end=True)
        txs, truncated = await self._history(PATH_TRANSACTIONS, Transaction, lambda t: t.dateTime, start=start, end=end)
        if type_:
            txs = [t for t in txs if (t.type or "").upper() == type_.upper()]
        totals: dict[str, float] = defaultdict(float)
        for t in txs:
            totals[f"{t.type}:{t.currency}"] += t.amount or 0
        shown = txs[:limit]
        return {
            "count": len(txs),
            "totals_by_type": {k: a.r(v) for k, v in sorted(totals.items())},
            "returned": len(shown),
            "truncated_history": truncated,
            "transactions": [_dump(t) for t in shown] if raw else [self._transaction_row(t) for t in shown],
        }

    async def get_pies(self, raw: bool = False) -> dict:
        pies = _parse_list(Pie, await self.client.get(PATH_PIES))
        if raw:
            return {"count": len(pies), "pies": [_dump(p) for p in pies]}
        rows = [
            {
                k: v
                for k, v in {
                    "id": p.id,
                    "invested": a.r(p.result.priceAvgInvestedValue),
                    "value": a.r(p.result.priceAvgValue),
                    "result": a.r(p.result.priceAvgResult),
                    "result_pct": a.r((p.result.priceAvgResultCoef or 0) * 100)
                    if p.result.priceAvgResultCoef is not None
                    else None,
                    "uninvested_cash": a.r(p.cash),
                    "dividends_gained": a.r(p.dividendDetails.gained),
                    "goal_progress_pct": a.r((p.progress or 0) * 100) if p.progress is not None else None,
                    "status": p.status,
                }.items()
                if v is not None
            }
            for p in pies
        ]
        return {
            "count": len(rows),
            "note": "Pie names are only returned by get_pie(id). The Pies API is deprecated by Trading 212.",
            "pies": rows,
        }

    async def get_pie(self, pie_id: int, raw: bool = False) -> dict:
        detail = _parse(PieDetail, await self.client.get(f"{PATH_PIES}/{int(pie_id)}"))
        if raw:
            return _dump(detail)
        s = detail.settings
        return {
            "id": s.id or pie_id,
            "name": s.name,
            "goal": s.goal,
            "dividend_action": s.dividendCashAction,
            "created": _day(s.creationDate),
            "end_date": _day(s.endDate),
            "instruments": [
                {
                    k: v
                    for k, v in {
                        "ticker": i.ticker,
                        "target_pct": a.r((i.expectedShare or 0) * 100) if i.expectedShare is not None else None,
                        "current_pct": a.r((i.currentShare or 0) * 100) if i.currentShare is not None else None,
                        "quantity": i.ownedQuantity,
                        "invested": a.r(i.result.priceAvgInvestedValue),
                        "value": a.r(i.result.priceAvgValue),
                        "result": a.r(i.result.priceAvgResult),
                        "issues": [x.get("name") for x in i.issues] or None,
                    }.items()
                    if v is not None
                }
                for i in detail.instruments
            ],
        }

    async def search_instruments(self, query: str, limit: int = 10, type_: str | None = None) -> dict:
        q = query.strip().lower()
        if len(q) < 1:
            raise ValueError("query is required")
        meta, warnings = await self._instrument_meta()
        if not meta:
            raise Trading212Error(warnings[0] if warnings else "Instrument metadata is empty.")
        scored: list[tuple[int, str, dict]] = []
        for ticker, m in meta.items():
            if type_ and m["type"].upper() != type_.upper():
                continue
            t = ticker.lower()
            name = (m["name"] or "").lower()
            short = (m["short_name"] or "").lower()
            isin = (m["isin"] or "").lower()
            score = 0
            if q in (t, isin):
                score = 100
            elif q == short or t.split("_")[0] == q:
                score = 90
            elif name == q:
                score = 85
            elif t.startswith(q):
                score = 70
            elif name.startswith(q):
                score = 60
            elif q in name or q in short:
                score = 40
            if score:
                scored.append((score, ticker, m))
        scored.sort(key=lambda s: (-s[0], len(s[2]["name"] or ""), s[1]))
        results = [
            {
                "ticker": ticker,
                "name": m["name"],
                "short_name": m["short_name"],
                "isin": m["isin"],
                "type": m["type"],
                "currency": m["currency"],
                "exchange": m["exchange"],
            }
            for _, ticker, m in scored[:limit]
        ]
        out: dict[str, Any] = {"matches": len(scored), "results": results}
        if warnings:
            out["warnings"] = warnings
        return out

    # ---------- analytics ----------

    async def portfolio_overview(self, top_n: int = 5, raw: bool = False) -> dict:
        summary, positions, (meta, warnings) = await asyncio.gather(
            self._summary(), self._positions(), self._instrument_meta()
        )
        total = sum(p.walletImpact.currentValue or 0 for p in positions)
        cost = sum(p.walletImpact.totalCost or 0 for p in positions)
        upnl = sum(p.walletImpact.unrealizedProfitLoss or 0 for p in positions)
        rows = sorted((self._position_row(p, total, meta) for p in positions), key=lambda x: -(x.get("value") or 0))

        def m(p: Position, key: str) -> str:
            return meta.get(p.ticker, {}).get(key) or "UNKNOWN"

        values = [(p, p.walletImpact.currentValue or 0) for p in positions]
        ranked = sorted(
            (x for x in rows if x.get("unrealized_pnl_pct") is not None),
            key=lambda x: -x["unrealized_pnl_pct"],
        )
        brief = ("ticker", "name", "value", "unrealized_pnl", "unrealized_pnl_pct")
        out: dict[str, Any] = {
            "as_of": self._now().isoformat(timespec="seconds"),
            "account_currency": summary.currency,
            "totals": {
                "account_value": a.r(summary.totalValue),
                "positions_value": a.r(total),
                "cost_basis": a.r(cost),
                "unrealized_pnl": a.r(upnl),
                "unrealized_pnl_pct": a.pct(upnl, cost),
                "realized_pnl_all_time": a.r(summary.investments.realizedProfitLoss),
                "cash_available": a.r(summary.cash.availableToTrade),
                "cash_pct_of_account": a.pct(summary.cash.availableToTrade, summary.totalValue),
            },
            "positions_count": len(positions),
            "allocation": {
                "by_type": a.allocation(((m(p, "type"), v) for p, v in values), total),
                "by_currency": a.allocation(((p.instrument.currency or "UNKNOWN", v) for p, v in values), total),
                "by_exchange": a.allocation(((m(p, "exchange"), v) for p, v in values), total),
            },
            "top_performers": [{k: x.get(k) for k in brief} for x in ranked[:top_n]],
            "bottom_performers": [{k: x.get(k) for k in brief} for x in reversed(ranked[-top_n:])] if ranked else [],
            "positions": rows
            if raw
            else [
                {k: x.get(k) for k in ("ticker", "value", "weight_pct", "unrealized_pnl_pct") if k in x} for x in rows
            ],
        }
        if warnings:
            out["warnings"] = warnings
        return out

    async def dividend_report(self, period: str = "ttm", raw: bool = False) -> dict:
        now = self._now()
        today = now.date()
        ttm_start = datetime.combine(today - timedelta(days=365), datetime.min.time(), tzinfo=UTC)
        p = period.strip().lower()
        if p == "ttm":
            start, end = ttm_start, now
        elif p == "ytd":
            start, end = datetime(today.year, 1, 1, tzinfo=UTC), now
        elif p == "all":
            start, end = None, now
        elif p.isdigit() and len(p) == 4:
            start = datetime(int(p), 1, 1, tzinfo=UTC)
            end = datetime(int(p), 12, 31, 23, 59, 59, tzinfo=UTC)
        else:
            raise ValueError("period must be 'ttm', 'ytd', 'all' or a year like '2025'")

        fetch_from = min(start, ttm_start) if start else None
        divs, truncated = await self._history(PATH_DIVIDENDS, Dividend, lambda d: d.paidOn, start=fetch_from, end=now)
        in_period = [d for d in divs if a.in_range(d.paidOn, start, end)]
        ttm = [d for d in divs if a.in_range(d.paidOn, ttm_start, now)]

        by_month: dict[str, float] = defaultdict(float)
        by_ticker: dict[str, dict] = {}
        for d in in_period:
            by_month[a.month_key(a.aware(d.paidOn))] += d.amount or 0  # type: ignore[arg-type]
            key = d.ticker or "?"
            entry = by_ticker.setdefault(
                key, {"ticker": key, "name": d.instrument.name if d.instrument else None, "total": 0.0, "count": 0}
            )
            entry["total"] += d.amount or 0
            entry["count"] += 1

        if start and in_period:
            month_end = min(end, now).date()
            months = a.months_between(start.date(), month_end)
        else:
            months = sorted(by_month)
        total = sum(d.amount or 0 for d in in_period)
        ttm_total = sum(d.amount or 0 for d in ttm)
        out: dict[str, Any] = {
            "period": p,
            "from": start.date().isoformat()
            if start
            else (_day(min((d.paidOn for d in in_period if d.paidOn), default=None))),
            "to": min(end, now).date().isoformat(),
            "currency": next((d.currency for d in divs if d.currency), None),
            "total": a.r(total),
            "payments": len(in_period),
            "trailing_12m_income": a.r(ttm_total),
            "trailing_12m_monthly_avg": a.r(ttm_total / 12),
            "by_month": [{"month": mo, "total": a.r(by_month.get(mo, 0.0))} for mo in months],
            "by_ticker": [
                {**e, "total": a.r(e["total"]), "share_pct": a.pct(e["total"], total)}
                for e in sorted(by_ticker.values(), key=lambda e: -e["total"])
            ],
            "truncated_history": truncated,
        }
        if raw:
            out["dividends"] = [self._dividend_row(d) for d in in_period]
        if truncated:
            out["warning"] = "History hit the page cap; older dividends may be missing."
        return out

    async def net_deposits_and_returns(self) -> dict:
        summary, (txs, truncated) = await asyncio.gather(
            self._summary(),
            self._history(PATH_TRANSACTIONS, Transaction, lambda t: t.dateTime, start=None, end=None),
        )
        ccy = summary.currency
        now = self._now()
        deposits = withdrawals = transfers = fees = interest = 0.0
        flows: list[tuple[datetime, float]] = []
        skipped: dict[str, int] = defaultdict(int)
        first: datetime | None = None
        for t in txs:
            amount, typ = t.amount or 0.0, (t.type or "").upper()
            if ccy and t.currency and t.currency != ccy:
                skipped[f"{typ}:{t.currency}"] += 1
                continue
            when = a.aware(t.dateTime)
            if typ == "DEPOSIT":
                deposits += abs(amount)
                flow = -abs(amount)
            elif typ == "WITHDRAW":
                withdrawals += abs(amount)
                flow = abs(amount)
            elif typ == "TRANSFER":
                transfers += amount
                flow = -amount
            elif typ == "FEE":
                fees += abs(amount)
                continue
            elif typ in ("INTEREST_ON_FREE_CASH", "LENDING_INTEREST"):
                interest += amount
                continue
            else:
                continue
            if when:
                flows.append((when, flow))
                first = when if first is None or when < first else first

        net = deposits - withdrawals + transfers
        value = summary.totalValue
        gain = (value - net) if value is not None else None
        mwr = a.xirr([*flows, (now, value)]) if value is not None and flows else None
        years = (now - first).days / 365.25 if first else None
        warnings: list[str] = []
        if truncated:
            warnings.append("Transaction history hit the page cap; totals may be incomplete.")
        if skipped:
            warnings.append(f"Skipped flows not in account currency {ccy}: {dict(skipped)}")
        if mwr is None and flows:
            warnings.append("Money-weighted return could not be computed from these flows.")
        out: dict[str, Any] = {
            "account_currency": ccy,
            "first_flow": _day(first),
            "deposits": a.r(deposits),
            "withdrawals": a.r(withdrawals),
            "transfers_net": a.r(transfers),
            "net_deposits": a.r(net),
            "current_account_value": a.r(value),
            "total_gain": a.r(gain),
            "simple_return_pct": a.pct(gain, net) if net > 0 else None,
            "money_weighted_return_annualized_pct": a.r(mwr * 100) if mwr is not None else None,
            "money_weighted_years": a.r(years, 2),
            "realized_pnl_all_time": a.r(summary.investments.realizedProfitLoss),
            "unrealized_pnl": a.r(summary.investments.unrealizedProfitLoss),
            "fees_paid": a.r(fees),
            "interest_received": a.r(interest),
            "method": "simple = (value - net deposits) / net deposits; money-weighted = XIRR of dated "
            "deposits/withdrawals/transfers with current account value as final inflow.",
        }
        if warnings:
            out["warnings"] = warnings
        return out


def _t(ticker: str | None) -> str | None:
    return ticker.strip().upper() if ticker and ticker.strip() else None


async def _run(coro: Awaitable[T]) -> T:
    try:
        return await coro
    except Trading212Error as exc:
        raise ToolError(str(exc)) from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None


# ---------------------------------------------------------------- MCP wiring

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

DateArg = Annotated[str | None, Field(description="Inclusive start date, YYYY-MM-DD. Omit for no lower bound.")]
ToDateArg = Annotated[str | None, Field(description="Inclusive end date, YYYY-MM-DD. Omit for today.")]
TickerArg = Annotated[
    str | None, Field(description="Trading 212 ticker such as AAPL_US_EQ (use search_instruments to find it).")
]
RawArg = Annotated[bool, Field(description="Return the full upstream objects instead of the compact summary.")]


Limit10 = Annotated[int, Field(ge=1, le=50, description="Max results to return (default 10).")]
Limit50 = Annotated[int, Field(ge=1, le=500, description="Max rows to return (default 50).")]
Limit100 = Annotated[int, Field(ge=1, le=1000, description="Max rows to return (default 100).")]


def register_tools(mcp: MCPServer, service: Trading212Service) -> None:
    s = service

    def tool(name: str, title: str, description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return mcp.tool(name=name, title=title, description=description, annotations=READ_ONLY, structured_output=False)

    @tool(
        "get_account_summary",
        "Trading 212 account summary",
        "Trading 212 (read-only): account currency, total value, cash (available, reserved, uninvested "
        "in pies), invested cost basis, current value, unrealized and all-time realized P&L.",
    )
    async def get_account_summary() -> dict:
        return await _run(s.get_account_summary())

    @tool(
        "get_positions",
        "Trading 212 open positions",
        "Trading 212 (read-only): every open position with quantity, average/current price, value, cost, "
        "unrealized P&L and portfolio weight. For allocation and performance analysis prefer "
        "portfolio_overview.",
    )
    async def get_positions(
        sort_by: Annotated[
            Literal["value", "pnl", "pnl_pct", "ticker"], Field(description="Sort order (default value).")
        ] = "value",
        raw: RawArg = False,
    ) -> dict:
        return await _run(s.get_positions(sort_by, raw))

    @tool(
        "get_position",
        "Trading 212 single position",
        "Trading 212 (read-only): one open position. Accepts a T212 ticker (AAPL_US_EQ), a plain symbol "
        "(AAPL) or an ISIN.",
    )
    async def get_position(
        ticker: Annotated[str, Field(description="T212 ticker (AAPL_US_EQ), plain symbol (AAPL) or ISIN.")],
        raw: RawArg = False,
    ) -> dict:
        return await _run(s.get_position(ticker, raw))

    @tool(
        "get_order_history",
        "Trading 212 order history",
        "Trading 212 (read-only): historical orders (filled, cancelled, rejected) newest first, with fill "
        "price, net value, realized P&L and fees. Filter by date range and/or ticker.",
    )
    async def get_order_history(
        from_date: DateArg = None,
        to_date: ToDateArg = None,
        ticker: TickerArg = None,
        limit: Limit50 = 50,
        raw: RawArg = False,
    ) -> dict:
        return await _run(s.get_order_history(from_date, to_date, ticker, limit, raw))

    @tool(
        "get_dividends",
        "Trading 212 dividends",
        "Trading 212 (read-only): individual dividend payments newest first, in account currency. Filter by "
        "date range and/or ticker. For totals by month/ticker and trailing-12-month income use "
        "dividend_report.",
    )
    async def get_dividends(
        from_date: DateArg = None,
        to_date: ToDateArg = None,
        ticker: TickerArg = None,
        limit: Limit100 = 100,
        raw: RawArg = False,
    ) -> dict:
        return await _run(s.get_dividends(from_date, to_date, ticker, limit, raw))

    @tool(
        "get_transactions",
        "Trading 212 cash transactions",
        "Trading 212 (read-only): cash movements newest first: DEPOSIT, WITHDRAW, FEE, TRANSFER, "
        "INTEREST_ON_FREE_CASH, LENDING_INTEREST. Filter by date range and type. For deposits vs returns "
        "use net_deposits_and_returns.",
    )
    async def get_transactions(
        from_date: DateArg = None,
        to_date: ToDateArg = None,
        type: Annotated[  # noqa: A002 - public tool argument name
            Literal["DEPOSIT", "WITHDRAW", "FEE", "TRANSFER", "INTEREST_ON_FREE_CASH", "LENDING_INTEREST"] | None,
            Field(description="Only this transaction type."),
        ] = None,
        limit: Limit100 = 100,
        raw: RawArg = False,
    ) -> dict:
        return await _run(s.get_transactions(from_date, to_date, type, limit, raw))

    @tool(
        "get_pies",
        "Trading 212 pies",
        "Trading 212 (read-only): all pies with invested amount, value, return, uninvested cash and "
        "dividends. Names need get_pie(id).",
    )
    async def get_pies(raw: RawArg = False) -> dict:
        return await _run(s.get_pies(raw))

    @tool(
        "get_pie",
        "Trading 212 pie detail",
        "Trading 212 (read-only): one pie's name, settings and holdings with target vs current weight.",
    )
    async def get_pie(
        id: Annotated[int, Field(description="Pie id from get_pies.", ge=1)],  # noqa: A002
        raw: RawArg = False,
    ) -> dict:
        return await _run(s.get_pie(id, raw))

    @tool(
        "search_instruments",
        "Search Trading 212 instruments",
        "Trading 212 (read-only): find instruments by T212 ticker (AAPL_US_EQ), plain symbol, ISIN or name. "
        "Returns ticker, name, ISIN, type, currency and exchange. Uses metadata cached for 24h.",
    )
    async def search_instruments(
        query: Annotated[str, Field(min_length=1, description="Ticker, symbol, ISIN or part of the name.")],
        limit: Limit10 = 10,
        type: Annotated[  # noqa: A002
            str | None, Field(description="Optional instrument type filter, e.g. STOCK or ETF.")
        ] = None,
    ) -> dict:
        return await _run(s.search_instruments(query, limit, type))

    @tool(
        "portfolio_overview",
        "Trading 212 portfolio overview",
        "Trading 212 analytics (read-only): account totals, each position's weight and unrealized P&L, "
        "allocation by instrument type, currency and exchange, and top/bottom performers. Best first "
        "call for 'how is my portfolio doing' questions.",
    )
    async def portfolio_overview(
        top_n: Annotated[int, Field(ge=1, le=20, description="How many top/bottom performers.")] = 5,
        raw: Annotated[bool, Field(description="Include full per-position rows.")] = False,
    ) -> dict:
        return await _run(s.portfolio_overview(top_n, raw))

    @tool(
        "dividend_report",
        "Trading 212 dividend report",
        "Trading 212 analytics (read-only): dividend totals by month and by ticker for a period, plus "
        "trailing-12-month income and monthly average.",
    )
    async def dividend_report(
        period: Annotated[
            str, Field(description="'ttm' (last 12 months, default), 'ytd', 'all' or a year like '2025'.")
        ] = "ttm",
        raw: Annotated[bool, Field(description="Also list each payment in the period.")] = False,
    ) -> dict:
        return await _run(s.dividend_report(period, raw))

    @tool(
        "net_deposits_and_returns",
        "Trading 212 deposits vs returns",
        "Trading 212 analytics (read-only): total deposited, withdrawn and net deposits vs current account "
        "value, total gain, simple return and annualized money-weighted return (XIRR), plus fees and "
        "interest.",
    )
    async def net_deposits_and_returns() -> dict:
        return await _run(s.net_deposits_and_returns())
