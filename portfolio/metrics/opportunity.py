"""Opportunity cost of idle cash vs the portfolio's own return (nice-to-have).

Pure computation. Frames idle cash as **cash drag**: the dollars left on the table
and the return haircut from holding cash (earning its low sweep/Bank-Interest yield)
instead of the portfolio's invested return. The benchmark `portfolio_return` is the
invested-sleeve PORTFOLIO XIRR from the Performance tab.

Inherits the unvalidated sweep cash model (Decision 19); the cash yield is taken from
Bank Interest only (Roth IIAXX reinvest interest is not separately credited), so all
figures are best-effort estimates — surfaced via the `note` column, never as truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from portfolio.models import Transaction
from portfolio.parsers.utils import parse_date

SCOPE_ALL = "ALL"
NOTE = "sweep-model cash (Decision 19, unvalidated); cash yield = Bank Interest only"


@dataclass
class OpportunityCost:
    scope: str                       # account_number or "ALL"
    avg_idle_cash: float
    avg_total_value: float
    cash_weight: float
    portfolio_return: float | None   # r_inv (benchmark)
    cash_return: float | None        # r_cash (realized, annualized)
    opportunity_cost: float | None   # dollars left on the table over the window
    cash_drag: float | None          # return haircut = cash_weight × (r_inv − r_cash)
    window_start: date | None
    window_years: float
    note: str


def time_weighted_average(
    series: list[tuple[date, float]], start: date, end: date
) -> float:
    """Time-weighted average balance over [start, end] from step-change points.

    Each point's balance is held until the next point (or ``end``). Points outside
    the window are clamped. Falls back to the last balance for a zero-length window.
    """
    total_days = (end - start).days
    if total_days <= 0 or not series:
        return series[-1][1] if series else 0.0
    pts = sorted(series)
    weighted = 0.0
    for i, (d, balance) in enumerate(pts):
        seg_start = max(d, start)
        seg_end = pts[i + 1][0] if i + 1 < len(pts) else end
        seg_end = min(seg_end, end)
        if seg_end > seg_start:
            weighted += balance * (seg_end - seg_start).days
    return weighted / total_days


def _interest_income(txns: list[Transaction], account: str | None = None) -> float:
    return sum(
        tx.amount
        for tx in txns
        if tx.tx_type == "INTEREST" and (account is None or tx.account_number == account)
    )


def build_opportunity(
    account_state: dict[str, dict],
    cash_series: dict[str, list[tuple[date, float]]],
    txns: list[Transaction],
    portfolio_return: float | None,
    invested_value_by_account: dict[str, float],
    today: date,
) -> list[OpportunityCost]:
    """Opportunity-cost rows for the consolidated portfolio (`ALL`) + each account.

    `portfolio_return` is the invested-sleeve return all idle cash is benchmarked
    against. Per account, window = [init_date, today]; idle cash is the time-weighted
    average reconstructed balance; cash yield = Bank Interest / avg cash, annualized.
    """
    accounts = sorted(cash_series)

    def _init(account: str) -> date | None:
        state = account_state.get(account)
        return parse_date(state["init_date"]) if state else None

    def _row(scope, avg_cash, invested, interest, window_start) -> OpportunityCost:
        window_years = (
            max((today - window_start).days / 365.0, 0.0) if window_start else 0.0
        )
        total_value = avg_cash + invested
        cash_weight = avg_cash / total_value if total_value else 0.0
        cash_return = (
            (interest / avg_cash) / window_years
            if avg_cash and window_years
            else None
        )
        if portfolio_return is None:
            opp = drag = None
        else:
            excess = portfolio_return - (cash_return or 0.0)
            opp = avg_cash * excess * window_years
            drag = cash_weight * excess
        return OpportunityCost(
            scope=scope,
            avg_idle_cash=avg_cash,
            avg_total_value=total_value,
            cash_weight=cash_weight,
            portfolio_return=portfolio_return,
            cash_return=cash_return,
            opportunity_cost=opp,
            cash_drag=drag,
            window_start=window_start,
            window_years=window_years,
            note=NOTE,
        )

    avg_by_account = {
        acc: time_weighted_average(cash_series[acc], _init(acc), today)
        if _init(acc) else 0.0
        for acc in accounts
    }
    interest_by_account = {acc: _interest_income(txns, acc) for acc in accounts}

    all_start = min((d for d in (_init(a) for a in accounts) if d), default=None)
    rows = [_row(
        SCOPE_ALL,
        sum(avg_by_account.values()),
        sum(invested_value_by_account.values()),
        sum(interest_by_account.values()),
        all_start,
    )]
    for acc in accounts:
        rows.append(_row(
            acc,
            avg_by_account[acc],
            invested_value_by_account.get(acc, 0.0),
            interest_by_account[acc],
            _init(acc),
        ))
    return rows
