"""Unit tests for portfolio/metrics/opportunity.py (pure, offline)."""

from datetime import date

import pytest

from portfolio.metrics.opportunity import (
    SCOPE_ALL,
    build_opportunity,
    time_weighted_average,
)
from portfolio.models import Transaction


def _tx(tx_type, amount, d, account="A", symbol=None):
    return Transaction(
        trade_date=d, settlement_date=d, status="Settled",
        account_number=account, account_registration="CMA-Edge",
        tx_type=tx_type, description="", symbol=symbol,
        quantity=None, price=None, amount=amount, source_file="t.csv",
    )


# ---------------------------------------------------------------------------
# time_weighted_average
# ---------------------------------------------------------------------------

def test_time_weighted_average_constant_balance():
    series = [(date(2025, 1, 1), 1000.0)]
    assert time_weighted_average(series, date(2025, 1, 1), date(2025, 12, 31)) == 1000.0


def test_time_weighted_average_step_change_midyear():
    # 1000 for the first half, 2000 for the second (equal halves) -> 1500
    series = [(date(2025, 1, 1), 1000.0), (date(2025, 7, 2), 2000.0)]
    avg = time_weighted_average(series, date(2025, 1, 1), date(2025, 12, 31))
    assert avg == pytest.approx(1500.0)


def test_time_weighted_average_zero_window_falls_back_to_last():
    series = [(date(2025, 1, 1), 1000.0), (date(2025, 6, 1), 1500.0)]
    assert time_weighted_average(series, date(2025, 1, 1), date(2025, 1, 1)) == 1500.0


# ---------------------------------------------------------------------------
# build_opportunity
# ---------------------------------------------------------------------------

STATE = {"A": {"init_date": "1/1/2025", "bootstrap_source_file": "h.csv"}}


def test_build_opportunity_cost_and_drag_signs():
    series = {"A": [(date(2025, 1, 1), 1000.0)]}              # constant $1000 idle
    txns = [_tx("INTEREST", 20.0, date(2025, 6, 1))]          # $20 interest over the year
    rows = build_opportunity(
        STATE, series, txns, portfolio_return=0.10,
        invested_value_by_account={"A": 9000.0}, today=date(2026, 1, 1),
    )
    by_scope = {r.scope: r for r in rows}
    assert set(by_scope) == {SCOPE_ALL, "A"}
    a = by_scope["A"]
    assert a.avg_idle_cash == pytest.approx(1000.0)
    assert a.window_years == pytest.approx(1.0, abs=0.01)
    assert a.cash_return == pytest.approx(0.02, abs=1e-3)        # 20/1000 over 1y
    assert a.cash_weight == pytest.approx(0.1)                   # 1000 / 10000
    # excess = 0.10 - 0.02 = 0.08 -> $80 over the window, drag 0.008
    assert a.opportunity_cost == pytest.approx(80.0, abs=1.0)
    assert a.cash_drag == pytest.approx(0.008, abs=1e-3)


def test_build_opportunity_all_scope_first_and_aggregates():
    state = {
        "A": {"init_date": "1/1/2025"},
        "B": {"init_date": "1/1/2025"},
    }
    series = {
        "A": [(date(2025, 1, 1), 1000.0)],
        "B": [(date(2025, 1, 1), 3000.0)],
    }
    txns = [
        _tx("INTEREST", 20.0, date(2025, 6, 1), account="A"),
        _tx("INTEREST", 60.0, date(2025, 6, 1), account="B"),
    ]
    rows = build_opportunity(
        state, series, txns, portfolio_return=0.10,
        invested_value_by_account={"A": 9000.0, "B": 7000.0}, today=date(2026, 1, 1),
    )
    assert rows[0].scope == SCOPE_ALL
    assert rows[0].avg_idle_cash == pytest.approx(4000.0)        # 1000 + 3000
    assert rows[0].avg_total_value == pytest.approx(20000.0)     # 4000 + 16000


def test_build_opportunity_none_portfolio_return_blanks_cost():
    series = {"A": [(date(2025, 1, 1), 1000.0)]}
    rows = build_opportunity(
        STATE, series, [], portfolio_return=None,
        invested_value_by_account={"A": 9000.0}, today=date(2026, 1, 1),
    )
    a = next(r for r in rows if r.scope == "A")
    assert a.opportunity_cost is None
    assert a.cash_drag is None


def test_build_opportunity_no_interest_yields_zero_cash_return():
    series = {"A": [(date(2025, 1, 1), 1000.0)]}
    rows = build_opportunity(
        STATE, series, [], portfolio_return=0.10,
        invested_value_by_account={"A": 9000.0}, today=date(2026, 1, 1),
    )
    a = next(r for r in rows if r.scope == "A")
    assert a.cash_return == pytest.approx(0.0)
    # full portfolio return is then the excess
    assert a.cash_drag == pytest.approx(0.1 * 0.1)              # weight 0.1 × excess 0.10
