"""Pure calculation helpers for the analytics tools (no I/O)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime, time


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_day(value: str | None, *, end: bool = False) -> datetime | None:
    """Parse YYYY-MM-DD (or full ISO datetime) into an aware UTC datetime."""
    if value is None or value == "":
        return None
    try:
        if len(value) == 10:
            d = date.fromisoformat(value)
            return datetime.combine(d, time.max if end else time.min, tzinfo=UTC)
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        raise ValueError(f"Invalid date {value!r}; use YYYY-MM-DD.") from None


def in_range(dt: datetime | None, start: datetime | None, end: datetime | None) -> bool:
    dt = aware(dt)
    if dt is None:
        return start is None and end is None
    return (start is None or dt >= start) and (end is None or dt <= end)


def r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def pct(part: float | None, whole: float | None, digits: int = 2) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100, digits)


def allocation(rows: Iterable[tuple[str, float]], total: float) -> list[dict]:
    """Group (key, value) pairs into [{key, value, weight_pct}] sorted by value desc."""
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for key, value in rows:
        sums[key] += value
        counts[key] += 1
    return [
        {"key": k, "value": r(v), "weight_pct": pct(v, total), "positions": counts[k]}
        for k, v in sorted(sums.items(), key=lambda kv: -kv[1])
    ]


def month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def months_between(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def xirr(flows: list[tuple[datetime, float]]) -> float | None:
    """Annualised money-weighted return for dated cash flows (investor's view:
    contributions negative, withdrawals and final value positive). None if undefined."""
    flows = sorted(((aware(d), a) for d, a in flows if a), key=lambda f: f[0])
    if len(flows) < 2 or not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None
    t0 = flows[0][0]
    years = [((d - t0).total_seconds() / (365.25 * 86400), a) for d, a in flows]
    if years[-1][0] <= 0:
        return None

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** t for t, a in years)

    lo, hi = -0.9999, 1.0
    f_lo = npv(lo)
    while npv(hi) * f_lo > 0 and hi < 1e6:
        hi *= 2
    if npv(hi) * f_lo > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            break
        if f_mid * f_lo < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2
