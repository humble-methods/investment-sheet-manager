"""Per-position performance: money-weighted return (XIRR) lifetime + per-year Modified Dietz.

Pure computation — transactions, price history, and year-end share snapshots are
passed in (no network, no yfinance import). Two return flavors per scope:

  * **total** — dividends (and ADR fees / foreign tax withholding, net) included
  * **price** — dividends excluded (capital appreciation only)

Their difference is the income contribution. Lifetime uses **annualized XIRR**
(money-weighted, handles every lot's buy date); per calendar year uses
**non-annualized Modified Dietz** (avoids inflating partial-year holdings).

Sign conventions (the two methods differ deliberately):
  * XIRR consumes raw ``tx.amount`` — buys negative (money out), sells/dividends
    positive (money in), terminal current value positive.
  * Modified Dietz capital flows are contributions — a buy is ``-tx.amount`` (+cost,
    capital in), a sell is ``-tx.amount`` (−proceeds, capital out); dividends are
    income added to the numerator, not capital flows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from portfolio.models import Transaction

_BUY_TYPES = {"BUY", "INIT_BUY"}
# Income leg for total return: dividends plus the costs that ride on them (both
# fee types carry negative amounts, so summing nets them against the dividend).
_INCOME_TYPES = {"DIVIDEND", "ADR_FEE", "TAX_WITHHOLDING"}

PORTFOLIO = "PORTFOLIO"


@dataclass
class SymbolPerformance:
    symbol: str                       # ticker, or "PORTFOLIO" for the pooled row
    first_held: date | None
    current_value: float
    cost_basis: float
    lifetime_total_xirr: float | None
    lifetime_price_xirr: float | None
    income_contribution: float | None  # total − price (None if either is None)


@dataclass
class YearPerformance:
    symbol: str
    year: int
    begin_value: float
    end_value: float
    net_flows: float        # net capital deployed in the year (buys − sells)
    dividends: float        # net income recognized in the year
    total_return: float | None
    price_return: float | None


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------


def xirr(flows: list[tuple[date, float]], guess: float = 0.1) -> float | None:
    """Annualized money-weighted return for dated cash flows, or None.

    Newton's method with a bracketed bisection fallback. Returns None when there
    are fewer than two non-zero flows or they don't straddle zero (no sign change
    → no root). Rate is per year (365-day basis).
    """
    pts = [(d, float(a)) for d, a in flows if a]
    if len(pts) < 2:
        return None
    amounts = [a for _, a in pts]
    if min(amounts) >= 0 or max(amounts) <= 0:
        return None  # need both inflows and outflows
    t0 = min(d for d, _ in pts)
    years = [(d - t0).days / 365.0 for d, _ in pts]

    def npv(rate: float) -> float:
        return sum(a / (1.0 + rate) ** y for a, y in zip(amounts, years))

    def dnpv(rate: float) -> float:
        return sum(-y * a / (1.0 + rate) ** (y + 1.0) for a, y in zip(amounts, years))

    # Newton
    rate = guess
    for _ in range(100):
        slope = dnpv(rate)
        if slope == 0:
            break
        nxt = rate - npv(rate) / slope
        if nxt <= -0.9999:           # damp back toward the -1 singularity
            nxt = (rate - 0.9999) / 2.0
        if abs(nxt - rate) < 1e-9:
            rate = nxt
            break
        rate = nxt
    if rate > -1.0 and abs(npv(rate)) < 1e-4:
        return rate

    # Bisection fallback over a wide bracket
    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def modified_dietz(
    begin_value: float,
    end_value: float,
    capital_flows: list[tuple[date, float]],  # (date, contribution): buy +cost, sell −proceeds
    period_start: date,
    period_end: date,
    income: float = 0.0,                       # dividends(+net fees) for total; 0 for price
) -> float | None:
    """Non-annualized money-weighted period return, or None if the base is zero.

    R = (EV − BV − netflow + income) / (BV + Σ wᵢ·flowᵢ), with each flow weighted
    by the fraction of the period remaining after it.
    """
    total_days = (period_end - period_start).days
    if total_days <= 0:
        return None
    net_flow = 0.0
    weighted = 0.0
    for d, amt in capital_flows:
        net_flow += amt
        weighted += amt * (period_end - d).days / total_days
    base = begin_value + weighted
    if base == 0:
        return None
    return (end_value - begin_value - net_flow + income) / base


def _close_on_or_before(history, as_of: date) -> float | None:
    """Last weekly close on/before ``as_of`` from a PriceHistory-like object."""
    if history is None:
        return None
    cutoff = as_of.isoformat()
    best = None
    for d, c in zip(history.dates, history.closes):
        if d <= cutoff:
            best = c
        else:
            break  # dates are ascending
    return best


# ---------------------------------------------------------------------------
# Cash-flow builders
# ---------------------------------------------------------------------------


def lifetime_cashflows(
    txns: list[Transaction],
    terminal_date: date,
    terminal_value: float,
    *,
    include_dividends: bool,
) -> list[tuple[date, float]]:
    """Signed dated cash flows for a symbol's lifetime XIRR.

    Buys/sells contribute raw ``tx.amount``; dividends/fees are added only when
    ``include_dividends``; the terminal current value is a final positive flow.
    """
    flows: list[tuple[date, float]] = []
    for tx in txns:
        if tx.tx_type in _BUY_TYPES or tx.tx_type == "SELL":
            flows.append((tx.trade_date, tx.amount))
        elif include_dividends and tx.tx_type in _INCOME_TYPES:
            flows.append((tx.trade_date, tx.amount))
    if terminal_value:
        flows.append((terminal_date, terminal_value))
    return flows


def year_returns(
    txns: list[Transaction],
    year: int,
    begin_value: float,
    end_value: float,
    period_end: date,
) -> tuple[float | None, float | None, float, float]:
    """(total_return, price_return, net_flows, dividends) for one calendar year."""
    period_start = date(year, 1, 1)
    capital_flows: list[tuple[date, float]] = []
    net_flows = 0.0
    dividends = 0.0
    for tx in txns:
        if tx.trade_date.year != year:
            continue
        if tx.tx_type in _BUY_TYPES or tx.tx_type == "SELL":
            contribution = -tx.amount  # buy → +cost, sell → −proceeds
            capital_flows.append((tx.trade_date, contribution))
            net_flows += contribution
        elif tx.tx_type in _INCOME_TYPES:
            dividends += tx.amount
    total = modified_dietz(
        begin_value, end_value, capital_flows, period_start, period_end, income=dividends
    )
    price = modified_dietz(
        begin_value, end_value, capital_flows, period_start, period_end, income=0.0
    )
    return total, price, net_flows, dividends


def _first_held(txns: list[Transaction]) -> date | None:
    dates = [tx.trade_date for tx in txns if tx.tx_type in _BUY_TYPES]
    return min(dates) if dates else None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_performance(
    held_symbols: list[str],
    txns_by_symbol: dict[str, list[Transaction]],
    current_value: dict[str, float],
    cost_basis: dict[str, float],
    shares_by_yearend: dict[int, dict[str, float]],  # year → {symbol: shares at Dec 31}
    histories: dict,                                  # symbol → PriceHistory-like
    today: date,
) -> tuple[list[SymbolPerformance], list[YearPerformance]]:
    """Assemble the Performance summary (per symbol + PORTFOLIO) and per-year rows.

    Per-year rows are emitted only where prices are available: a year is skipped if
    its closing price is missing, or if shares were held at year-start but the
    opening price is missing (would understate the opening value). The first year a
    symbol is held legitimately opens at value 0.
    """
    summaries: list[SymbolPerformance] = []
    yearly: list[YearPerformance] = []

    for symbol in sorted(held_symbols):
        txns = txns_by_symbol.get(symbol, [])
        cur_val = current_value.get(symbol, 0.0)
        total_x = xirr(lifetime_cashflows(txns, today, cur_val, include_dividends=True))
        price_x = xirr(lifetime_cashflows(txns, today, cur_val, include_dividends=False))
        income = (
            total_x - price_x if total_x is not None and price_x is not None else None
        )
        summaries.append(SymbolPerformance(
            symbol, _first_held(txns), cur_val, cost_basis.get(symbol, 0.0),
            total_x, price_x, income,
        ))

        first = _first_held(txns)
        if first is None:
            continue
        history = histories.get(symbol)
        for year in range(first.year, today.year + 1):
            begin_shares = shares_by_yearend.get(year - 1, {}).get(symbol, 0.0)
            begin_close = _close_on_or_before(history, date(year - 1, 12, 31))
            if begin_shares and begin_close is None:
                continue  # held at open but no opening price → can't value the year
            begin_value = begin_shares * (begin_close or 0.0)

            if year < today.year:
                end_close = _close_on_or_before(history, date(year, 12, 31))
                if end_close is None:
                    continue  # no closing price this year
                end_value = shares_by_yearend.get(year, {}).get(symbol, 0.0) * end_close
                period_end = date(year, 12, 31)
            else:
                end_value = cur_val
                period_end = today

            total_r, price_r, net_flows, divs = year_returns(
                txns, year, begin_value, end_value, period_end
            )
            yearly.append(YearPerformance(
                symbol, year, begin_value, end_value, net_flows, divs, total_r, price_r,
            ))

    # PORTFOLIO: pooled XIRR over every symbol's flows + total terminal value.
    total_flows: list[tuple[date, float]] = []
    price_flows: list[tuple[date, float]] = []
    for symbol in held_symbols:
        txns = txns_by_symbol.get(symbol, [])
        cur_val = current_value.get(symbol, 0.0)
        total_flows += lifetime_cashflows(txns, today, cur_val, include_dividends=True)
        price_flows += lifetime_cashflows(txns, today, cur_val, include_dividends=False)
    p_total = xirr(total_flows)
    p_price = xirr(price_flows)
    p_income = (
        p_total - p_price if p_total is not None and p_price is not None else None
    )
    first_all = min(
        (s.first_held for s in summaries if s.first_held), default=None
    )
    summaries.append(SymbolPerformance(
        PORTFOLIO, first_all, sum(current_value.values()), sum(cost_basis.values()),
        p_total, p_price, p_income,
    ))

    return summaries, yearly
