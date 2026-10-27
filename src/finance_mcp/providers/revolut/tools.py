"""Revolut (via Enable Banking) MCP tools, all read-only.

`RevolutService` holds the logic (testable without MCP); `register_tools` exposes it.
"""

from __future__ import annotations

import asyncio
import calendar
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from finance_mcp.providers.revolut import categorize as cat
from finance_mcp.providers.revolut.client import EnableBankingClient, EnableBankingError, Fetched

T = TypeVar("T")

BOOKED_TYPES = ("CLBD", "ITBD", "OPBD", "XPCD", "PRCD", "VALU", "OTHR", "INFO")
AVAILABLE_TYPES = ("ITAV", "CLAV", "OPAV", "FWAV")
SKIP_STATUSES = {"RJCT", "CNCL"}
CONSENT_WARN_DAYS = 14
Period = Literal["today", "yesterday", "week", "month", "custom"]


def r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value + 0.0, digits)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ValueError(f"{field} must be YYYY-MM-DD") from None


def _iban(account: dict) -> str | None:
    ident = account.get("account_id") or {}
    if isinstance(ident, dict) and ident.get("iban"):
        return str(ident["iban"])
    for other in account.get("all_account_ids") or []:
        if isinstance(other, dict) and other.get("scheme_name") == "IBAN" and other.get("identification"):
            return str(other["identification"])
    return None


class RevolutService:
    def __init__(
        self,
        client: EnableBankingClient,
        *,
        labels: dict[str, str] | None = None,
        owner_names: list[str] | None = None,
        living_budget: float = 1500.0,
        living_account: str = "Living Expenses Monthly",
        tz: str = "Europe/Madrid",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client
        self.labels = {k.strip().lower(): v for k, v in (labels or {}).items()}
        self.owner_names = [n.strip().lower() for n in owner_names or [] if n.strip()]
        self.living_budget = living_budget
        self.living_account = living_account
        self.tz = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(UTC))

    def today(self) -> date:
        return self._now().astimezone(self.tz).date()

    # ------------------------------------------------------------ accounts

    async def _accounts(self) -> tuple[list[dict], list[str]]:
        """Every account in the consent, with details and label. Returns (accounts, notes)."""
        session = (await self.client.session()).value
        uids = [u for u in session.get("accounts") or [] if isinstance(u, str)]
        details = await asyncio.gather(*(self.client.account_details(u) for u in uids), return_exceptions=True)
        accounts, notes = [], []
        for uid, d in zip(uids, details, strict=True):
            if isinstance(d, BaseException):
                if not isinstance(d, EnableBankingError):
                    raise d
                notes.append(f"Details unavailable for account {uid}: {d}")
                info: dict = {}
            else:
                info = d.value if isinstance(d.value, dict) else {}
                if d.stale and d.note:
                    notes.append(d.note)
            accounts.append(self._account_row(uid, info))
        return accounts, notes

    def _account_row(self, uid: str, info: dict) -> dict:
        iban = _iban(info)
        keys = [uid, iban, info.get("identification_hash"), info.get("name"), info.get("product")]
        label = next((self.labels[k.lower()] for k in keys if k and k.lower() in self.labels), None)
        row = {
            "uid": uid,
            "label": label,
            "name": info.get("name"),
            "product": info.get("product"),
            "details": info.get("details"),
            "currency": info.get("currency"),
            "iban": iban,
            "cash_account_type": info.get("cash_account_type"),
            "usage": info.get("usage"),
            "identification_hash": info.get("identification_hash"),
        }
        row = {k: v for k, v in row.items() if v is not None}
        # Revolut sets `name` to the account holder on every account; pockets carry their name in `details`.
        row["kind"] = "pocket" if info.get("cash_account_type") == "SVGS" else "current"
        ccy_iban = " ".join(x for x in (info.get("currency"), f"…{iban[-4:]}" if iban else None) if x)
        row["display"] = label or info.get("details") or ccy_iban or info.get("product") or info.get("name") or uid[:8]
        return row

    async def _select(self, account: str | None) -> tuple[list[dict], list[str]]:
        accounts, notes = await self._accounts()
        if not account:
            return accounts, notes
        q = account.strip().lower()
        for field in ("uid", "label", "iban", "display", "details", "product", "name"):
            hits = [a for a in accounts if str(a.get(field, "")).lower().replace(" ", "") == q.replace(" ", "")]
            if hits:
                return hits, notes
        hits = [a for a in accounts if a.get("iban", "").endswith(q.upper()) or q in a["display"].lower()]
        if hits:
            return hits, notes
        choices = ", ".join(a["display"] for a in accounts) or "none"
        raise ValueError(f"No Revolut account matches {account!r}. Accounts: {choices}.")

    def _own_identifiers(self, accounts: list[dict]) -> list[str]:
        out = list(self.owner_names) + list(self.labels.values())
        for a in accounts:
            out += [str(a[k]) for k in ("name", "label", "iban", "product", "details") if a.get(k)]
        return [o.lower() for o in out]

    # ------------------------------------------------------------ tools

    async def list_accounts(self) -> dict:
        accounts, notes = await self._accounts()
        out: dict[str, Any] = {"count": len(accounts), "accounts": accounts}
        if notes:
            out["notes"] = notes
        return out

    async def get_balances(self, account: str | None = None) -> dict:
        accounts, notes = await self._select(account)
        results = await asyncio.gather(*(self.client.balances(a["uid"]) for a in accounts), return_exceptions=True)
        rows = []
        for a, res in zip(accounts, results, strict=True):
            if isinstance(res, BaseException):
                if not isinstance(res, EnableBankingError):
                    raise res
                rows.append({"account": a["display"], "uid": a["uid"], "error": str(res)})
                continue
            rows.append({"account": a["display"], "uid": a["uid"], **self._balance_row(res.value), **res.meta()})
        totals: dict[str, float] = defaultdict(float)
        for row in rows:
            if row.get("available") is not None and row.get("currency"):
                totals[row["currency"]] += row["available"]
        out: dict[str, Any] = {"balances": rows, "total_available_by_currency": {k: r(v) for k, v in totals.items()}}
        if notes:
            out["notes"] = notes
        return out

    @staticmethod
    def _balance_row(body: Any) -> dict:
        items = [b for b in (body or {}).get("balances") or [] if isinstance(b, dict)] if isinstance(body, dict) else []
        by_type: dict[str, dict] = {}
        for b in items:
            by_type.setdefault(str(b.get("balance_type")), b)

        def pick(types: tuple[str, ...]) -> dict | None:
            return next((by_type[t] for t in types if t in by_type), None)

        booked, available = pick(BOOKED_TYPES), pick(AVAILABLE_TYPES)
        amt = lambda b: _float((b.get("balance_amount") or {}).get("amount")) if b else None  # noqa: E731
        currency = next(((b.get("balance_amount") or {}).get("currency") for b in (booked, available) if b), None)
        return {
            "currency": currency,
            "booked": r(amt(booked)),
            "booked_type": booked.get("balance_type") if booked else None,
            "available": r(amt(available)),
            "available_type": available.get("balance_type") if available else None,
            "reference_date": (booked or available or {}).get("reference_date"),
            "all": [
                {
                    "type": b.get("balance_type"),
                    "name": b.get("name"),
                    "amount": r(amt(b)),
                    "currency": (b.get("balance_amount") or {}).get("currency"),
                }
                for b in items
            ],
        }

    async def _transactions(
        self, accounts: list[dict], start: date, end: date
    ) -> tuple[list[dict], list[dict], list[str]]:
        """Normalized transactions across accounts in [start, end], newest first."""
        results = await asyncio.gather(
            *(self.client.transactions(a["uid"], start) for a in accounts), return_exceptions=True
        )
        rows, meta, notes = [], [], []
        for a, res in zip(accounts, results, strict=True):
            if isinstance(res, BaseException):
                if not isinstance(res, EnableBankingError):
                    raise res
                notes.append(f"{a['display']}: {res}")
                continue
            meta.append({"account": a["display"], **res.meta()})
            for raw in res.value:
                row = normalize(raw, a)
                if row["date"] and start.isoformat() <= row["date"] <= end.isoformat():
                    rows.append(row)
        rows.sort(key=lambda t: (t["date"] or "", t.get("_order", 0)), reverse=True)
        return rows, meta, notes

    async def get_transactions(
        self,
        account: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        end = _day(to_date, "to_date") or self.today()
        start = _day(from_date, "from_date") or end - timedelta(days=30)
        if start > end:
            raise ValueError("from_date must be on or before to_date")
        try:
            offset = int(cursor or 0)
        except ValueError:
            raise ValueError("cursor must be the next_cursor value from a previous call") from None
        accounts, notes = await self._select(account)
        rows, meta, more_notes = await self._transactions(accounts, start, end)
        page = rows[offset : offset + limit]
        out: dict[str, Any] = {
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "total_count": len(rows),
            "returned": len(page),
            "next_cursor": str(offset + limit) if offset + limit < len(rows) else None,
            "transactions": [_public(t) for t in page],
            "sources": meta,
        }
        if notes or more_notes:
            out["notes"] = notes + more_notes
        return out

    def period_range(self, period: Period, from_date: str | None, to_date: str | None) -> tuple[date, date]:
        today = self.today()
        if period == "today":
            return today, today
        if period == "yesterday":
            y = today - timedelta(days=1)
            return y, y
        if period == "week":
            return today - timedelta(days=today.weekday()), today
        if period == "month":
            return today.replace(day=1), today
        if period == "custom":
            start = _day(from_date, "from_date")
            if start is None:
                raise ValueError("period='custom' needs from_date (YYYY-MM-DD)")
            end = _day(to_date, "to_date") or today
            if start > end:
                raise ValueError("from_date must be on or before to_date")
            return start, end
        raise ValueError("period must be today, yesterday, week, month or custom")

    def classify(self, t: dict, own: list[str]) -> str:
        """'internal', 'trading212', 'spend' or 'income'."""
        text = f"{t.get('counterparty') or ''} {t.get('description') or ''}"
        if cat.is_trading212(text):
            return "trading212"
        if cat.is_internal(text, own):
            return "internal"
        return "spend" if t["amount"] < 0 else "income"

    async def spending_summary(
        self,
        period: Period = "month",
        from_date: str | None = None,
        to_date: str | None = None,
        account: str | None = None,
    ) -> dict:
        start, end = self.period_range(period, from_date, to_date)
        accounts, notes = await self._select(account)
        all_accounts = accounts if account is None else (await self._accounts())[0]
        own = self._own_identifiers(all_accounts)
        rows, meta, more_notes = await self._transactions(accounts, start, end)

        per_ccy: dict[str, dict[str, Any]] = {}
        for t in rows:
            if t.get("status") in SKIP_STATUSES:
                continue
            s = per_ccy.setdefault(
                t["currency"] or "?",
                {
                    "out": 0.0,
                    "in": 0.0,
                    "cats": defaultdict(lambda: [0.0, 0]),
                    "merchants": defaultdict(lambda: [0.0, 0]),
                    "excluded": defaultdict(lambda: {"count": 0, "out": 0.0, "in": 0.0}),
                },
            )
            kind = self.classify(t, own)
            if kind in ("internal", "trading212"):
                ex = s["excluded"]["internal_transfers" if kind == "internal" else "trading212"]
                ex["count"] += 1
                ex["out" if t["amount"] < 0 else "in"] += abs(t["amount"])
            elif kind == "spend":
                amount = -t["amount"]
                s["out"] += amount
                c = s["cats"][t["category"]]
                c[0] += amount
                c[1] += 1
                m = s["merchants"][cat.merchant_key(t["merchant"])]
                m[0] += amount
                m[1] += 1
            else:
                s["in"] += t["amount"]

        summaries = []
        for ccy, s in sorted(per_ccy.items(), key=lambda kv: -kv[1]["out"]):
            out_total = s["out"]
            summaries.append(
                {
                    "currency": ccy,
                    "total_out": r(out_total),
                    "total_in": r(s["in"]),
                    "net": r(s["in"] - out_total),
                    "by_category": [
                        {"category": k, "amount": r(v[0]), "count": v[1], "pct": r(100 * v[0] / out_total, 1)}
                        for k, v in sorted(s["cats"].items(), key=lambda kv: -kv[1][0])
                        if out_total
                    ],
                    "top_merchants": [
                        {"merchant": k, "amount": r(v[0]), "count": v[1]}
                        for k, v in sorted(s["merchants"].items(), key=lambda kv: -kv[1][0])[:10]
                    ],
                    "excluded": {
                        k: {"count": v["count"], "out": r(v["out"]), "in": r(v["in"])} for k, v in s["excluded"].items()
                    },
                }
            )
        out: dict[str, Any] = {
            "period": period,
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "accounts": [a["display"] for a in accounts],
            "summaries": summaries,
            "method": "Spending = debits excluding transfers between your own accounts/pockets and transfers to "
            "Trading 212 (shown under 'excluded'). Categories come from merchant category codes when "
            "present, otherwise merchant-name rules. Pending card payments are included.",
            "sources": meta,
        }
        if notes or more_notes:
            out["notes"] = notes + more_notes
        return out

    async def budget_status(self) -> dict:
        """Month-to-date pace of the living-expenses pocket against LIVING_BUDGET."""
        today = self.today()
        start = today.replace(day=1)
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        all_accounts, notes = await self._accounts()
        try:
            accounts, _ = await self._select(self.living_account)
        except ValueError:
            choices = ", ".join(a["display"] for a in all_accounts) or "none"
            raise ValueError(
                f"Living-expenses account {self.living_account!r} not found. Set LIVING_ACCOUNT to one of: {choices}."
            ) from None
        own = self._own_identifiers(all_accounts)
        rows, meta, more_notes = await self._transactions(accounts, start, today)

        spent = refunds = 0.0
        cats: dict[str, float] = defaultdict(float)
        merchants: dict[str, float] = defaultdict(float)
        for t in rows:
            if t.get("status") in SKIP_STATUSES or t["currency"] not in (None, accounts[0].get("currency")):
                continue
            kind = self.classify(t, own)
            if kind == "spend":
                spent += -t["amount"]
                cats[t["category"]] += -t["amount"]
                merchants[cat.merchant_key(t["merchant"])] += -t["amount"]
            elif kind == "income":
                refunds += t["amount"]

        net = max(spent - refunds, 0.0)
        budget = self.living_budget
        pct_elapsed = 100 * today.day / days_in_month
        pct_used = 100 * net / budget
        projected = net / today.day * days_in_month
        remaining = budget - net
        days_left = days_in_month - today.day
        if net > budget:
            status = "over_budget"
        elif projected > budget:
            status = "over_pace"
        else:
            status = "on_track"
        out: dict[str, Any] = {
            "month": start.strftime("%Y-%m"),
            "account": accounts[0]["display"],
            "currency": accounts[0].get("currency"),
            "budget": r(budget),
            "spent": r(net),
            "gross_spend": r(spent),
            "refunds": r(refunds),
            "remaining": r(remaining),
            "pct_month_elapsed": r(pct_elapsed, 1),
            "pct_budget_used": r(pct_used, 1),
            "projected_month_end": r(projected),
            "projected_vs_budget": r(projected - budget),
            "daily_allowance_rest_of_month": r(remaining / days_left) if days_left and remaining > 0 else None,
            "days_left": days_left,
            "status": status,
            "by_category": [{"category": k, "amount": r(v)} for k, v in sorted(cats.items(), key=lambda kv: -kv[1])],
            "top_merchants": [
                {"merchant": k, "amount": r(v)} for k, v in sorted(merchants.items(), key=lambda kv: -kv[1])[:5]
            ],
            "method": f"Living expenses = card payments and transfers out of the {accounts[0]['display']} pocket "
            "this month, minus refunds into it. Top-ups and moves between your own accounts are ignored. "
            "Projection is a straight-line extrapolation of the daily average so far.",
            "sources": meta,
        }
        if notes or more_notes:
            out["notes"] = notes + more_notes
        return out

    async def consent_status(self) -> dict:
        fetched: Fetched = await self.client.session()
        session = fetched.value if isinstance(fetched.value, dict) else {}
        valid_until = (session.get("access") or {}).get("valid_until")
        status = session.get("status")
        out: dict[str, Any] = {
            "status": status,
            "valid_until": valid_until,
            "accounts_covered": len(session.get("accounts") or []),
            "bank": (session.get("aspsp") or {}).get("name"),
        }
        expires = _parse_ts(valid_until)
        if expires:
            seconds = (expires - self._now()).total_seconds()
            out["days_left"] = max(int(seconds // 86400), 0)
            if seconds <= 0:
                out["warning"] = "Consent has expired. Re-link to restore Revolut data."
            elif seconds <= CONSENT_WARN_DAYS * 86400:
                out["warning"] = f"Consent expires in {out['days_left']} days. Re-link soon."
        if status and status != "AUTHORIZED":
            out["warning"] = f"Session status is {status}; Revolut data will not load until you re-link."
        if "warning" in out:
            out["how_to_relink"] = (
                "Run `uv run python scripts/link_revolut.py`, open the printed URL, approve in Revolut, paste "
                "the redirect URL back, then update ENABLE_BANKING_SESSION_ID locally and on Railway."
            )
        return out


def normalize(raw: dict, account: dict) -> dict:
    amt = raw.get("transaction_amount") or {}
    value = abs(_float(amt.get("amount")) or 0.0)
    debit = raw.get("credit_debit_indicator") == "DBIT" or (
        raw.get("credit_debit_indicator") is None and (_float(amt.get("amount")) or 0) < 0
    )
    creditor = (raw.get("creditor") or {}).get("name")
    debtor = (raw.get("debtor") or {}).get("name")
    counterparty = (creditor or debtor) if debit else (debtor or creditor)
    remittance = raw.get("remittance_information") or []
    description = " ".join(str(x) for x in remittance if x) if isinstance(remittance, list) else str(remittance)
    btc = raw.get("bank_transaction_code") or {}
    merchant = counterparty or description or btc.get("description") or "Unknown"
    return {
        "date": raw.get("booking_date") or raw.get("value_date") or raw.get("transaction_date"),
        "amount": r(-value if debit else value),
        "currency": amt.get("currency"),
        "merchant": merchant,
        "counterparty": counterparty,
        "description": description or None,
        "category": cat.category_for(f"{merchant} {description}", raw.get("merchant_category_code")),
        "status": raw.get("status"),
        "account": account["display"],
        "id": raw.get("entry_reference") or raw.get("transaction_id"),
        "type": btc.get("description") or btc.get("code"),
    }


def _public(t: dict) -> dict:
    return {k: v for k, v in t.items() if v is not None and not k.startswith("_")}


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _run(coro: Awaitable[T]) -> T:
    try:
        return await coro
    except (EnableBankingError, ValueError) as exc:
        raise ToolError(str(exc)) from None


# ---------------------------------------------------------------- MCP wiring

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

AccountArg = Annotated[
    str | None,
    Field(description="Account label, name, IBAN, last 4 IBAN digits or uid (see list_accounts). Omit for all."),
]
FromArg = Annotated[str | None, Field(description="Inclusive start date, YYYY-MM-DD.")]
ToArg = Annotated[str | None, Field(description="Inclusive end date, YYYY-MM-DD. Omit for today.")]


def register_tools(mcp: MCPServer, service: RevolutService) -> None:
    s = service

    def tool(name: str, title: str, description: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return mcp.tool(name=name, title=title, description=description, annotations=READ_ONLY, structured_output=False)

    @tool(
        "list_accounts",
        "Revolut accounts",
        "Revolut (read-only): every account/pocket in the bank consent, with your labels, currency and IBAN.",
    )
    async def list_accounts() -> dict:
        return await _run(s.list_accounts())

    @tool(
        "get_balances",
        "Revolut balances",
        "Revolut (read-only): booked and available balance per account, plus totals per currency. Data is "
        "cached for a few hours because the bank allows only ~4 background refreshes a day.",
    )
    async def get_balances(account: AccountArg = None) -> dict:
        return await _run(s.get_balances(account))

    @tool(
        "revolut_get_transactions",
        "Revolut transactions",
        "Revolut (read-only): bank transactions newest first with signed amount (negative = money out), "
        "merchant/counterparty, description, date and a rule-based category. Default range: last 30 days. "
        "Paginated: pass next_cursor back as cursor. For Trading 212 cash movements use get_transactions.",
    )
    async def revolut_get_transactions(
        account: AccountArg = None,
        from_date: FromArg = None,
        to_date: ToArg = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Rows per page (default 50).")] = 50,
        cursor: Annotated[str | None, Field(description="next_cursor from the previous page.")] = None,
    ) -> dict:
        return await _run(s.get_transactions(account, from_date, to_date, limit, cursor))

    @tool(
        "spending_summary",
        "Revolut spending summary",
        "Revolut analytics (read-only): spend by category (groceries, eating out, transport, subscriptions, "
        "shopping, travel, other), top merchants and total in vs out. Excludes transfers between your own "
        "accounts/pockets and to Trading 212. Periods are in local time: today, yesterday, week (Monday to "
        "today), month (1st to today) or custom with from_date/to_date.",
    )
    async def spending_summary(
        period: Annotated[Period, Field(description="today | yesterday | week | month | custom")] = "month",
        from_date: FromArg = None,
        to_date: ToArg = None,
        account: AccountArg = None,
    ) -> dict:
        return await _run(s.spending_summary(period, from_date, to_date, account))

    @tool(
        "budget_status",
        "Revolut living budget",
        "Revolut analytics (read-only): this month's living expenses (the living-expenses pocket) against the "
        "monthly LIVING_BUDGET: spent so far, % of month elapsed vs % of budget used, projected month-end "
        "total, remaining money and a daily allowance for the rest of the month.",
    )
    async def budget_status() -> dict:
        return await _run(s.budget_status())

    @tool(
        "consent_status",
        "Revolut consent status",
        "Revolut (read-only): whether the bank consent is active and how many days are left before it must "
        "be renewed. Check this in reviews and warn the owner when fewer than 14 days remain.",
    )
    async def consent_status() -> dict:
        return await _run(s.consent_status())
