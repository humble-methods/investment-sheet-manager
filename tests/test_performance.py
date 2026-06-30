"""Unit tests for portfolio/metrics/performance.py (pure, offline)."""

from datetime import date

import pytest

from portfolio.market.yfinance_client import PriceHistory
from portfolio.metrics.performance import (
    PORTFOLIO,
    _close_on_or_before,
    build_performance,
    lifetime_cashflows,
    modified_dietz,
    xirr,
    year_returns,
)
from portfolio.models import Transaction


def _tx(tx_type, symbol, d, amount, quantity=None, price=None, account="11A-1"):
    return Transaction(
        trade_date=d, settlement_date=d, status="Settled",
        account_number=account, account_registration="CMA-Edge",
        tx_type=tx_type, description="", symbol=symbol,
        quantity=quantity, price=price, amount=amount, source_file="t.csv",
    )


def _npv(rate, flows):
    t0 = min(d for d, _ in flows)
    return sum(a / (1.0 + rate) ** ((d - t0).days / 365.0) for d, a in flows)


# ---------------------------------------------------------------------------
# xirr
# ---------------------------------------------------------------------------

def test_xirr_ten_percent_one_year():
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 110.0)]
    assert xirr(flows) == pytest.approx(0.10, abs=1e-4)


def test_xirr_doubling_one_year():
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 200.0)]
    assert xirr(flows) == pytest.approx(1.0, abs=1e-4)


def test_xirr_loss_half_one_year():
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 50.0)]
    assert xirr(flows) == pytest.approx(-0.5, abs=1e-4)


def test_xirr_multiflow_is_self_consistent():
    flows = [
        (date(2024, 1, 1), -100.0),
        (date(2025, 1, 1), -100.0),
        (date(2026, 1, 1), 220.0),
    ]
    r = xirr(flows)
    assert r is not None
    assert _npv(r, flows) == pytest.approx(0.0, abs=1e-4)


def test_xirr_all_negative_returns_none():
    assert xirr([(date(2025, 1, 1), -100.0), (date(2026, 1, 1), -50.0)]) is None


def test_xirr_all_positive_returns_none():
    assert xirr([(date(2025, 1, 1), 100.0), (date(2026, 1, 1), 50.0)]) is None


def test_xirr_single_flow_returns_none():
    assert xirr([(date(2025, 1, 1), -100.0)]) is None


# ---------------------------------------------------------------------------
# modified_dietz
# ---------------------------------------------------------------------------

def test_modified_dietz_no_flows():
    r = modified_dietz(100.0, 110.0, [], date(2025, 1, 1), date(2025, 12, 31))
    assert r == pytest.approx(0.10)


def test_modified_dietz_income_adds_to_numerator():
    r = modified_dietz(100.0, 110.0, [], date(2025, 1, 1), date(2025, 12, 31), income=5.0)
    assert r == pytest.approx(0.15)


def test_modified_dietz_midyear_flow_is_time_weighted():
    # buy +100 halfway through the year: weight 0.5, base = 50
    start, end = date(2025, 1, 1), date(2025, 12, 31)
    mid = date(2025, 7, 2)  # 182 of 364 days remain
    r = modified_dietz(0.0, 110.0, [(mid, 100.0)], start, end)
    w = (end - mid).days / (end - start).days
    assert r == pytest.approx((110.0 - 100.0) / (w * 100.0))


def test_modified_dietz_zero_base_returns_none():
    assert modified_dietz(0.0, 0.0, [], date(2025, 1, 1), date(2025, 12, 31)) is None


# ---------------------------------------------------------------------------
# lifetime_cashflows
# ---------------------------------------------------------------------------

def test_lifetime_cashflows_total_includes_dividends_and_fees():
    txns = [
        _tx("BUY", "AAA", date(2024, 1, 2), -1000.0),
        _tx("DIVIDEND", "AAA", date(2025, 3, 1), 20.0),
        _tx("ADR_FEE", "AAA", date(2025, 3, 1), -2.0),
    ]
    total = lifetime_cashflows(txns, date(2026, 6, 16), 1500.0, include_dividends=True)
    assert (date(2025, 3, 1), 20.0) in total
    assert (date(2025, 3, 1), -2.0) in total      # fee nets against income
    assert (date(2026, 6, 16), 1500.0) in total   # terminal value


def test_lifetime_cashflows_price_excludes_dividends():
    txns = [
        _tx("BUY", "AAA", date(2024, 1, 2), -1000.0),
        _tx("DIVIDEND", "AAA", date(2025, 3, 1), 20.0),
    ]
    price = lifetime_cashflows(txns, date(2026, 6, 16), 1500.0, include_dividends=False)
    assert price == [(date(2024, 1, 2), -1000.0), (date(2026, 6, 16), 1500.0)]


# ---------------------------------------------------------------------------
# year_returns
# ---------------------------------------------------------------------------

def test_year_returns_extracts_flows_and_matches_modified_dietz():
    txns = [
        _tx("BUY", "AAA", date(2025, 7, 2), -500.0),     # contribution +500
        _tx("DIVIDEND", "AAA", date(2025, 9, 1), 30.0),
        _tx("BUY", "AAA", date(2024, 1, 1), -100.0),     # different year, ignored
    ]
    total, price, net_flows, divs = year_returns(
        txns, 2025, 1000.0, 1200.0, date(2025, 12, 31)
    )
    assert net_flows == pytest.approx(500.0)
    assert divs == pytest.approx(30.0)
    expected_total = modified_dietz(
        1000.0, 1200.0, [(date(2025, 7, 2), 500.0)],
        date(2025, 1, 1), date(2025, 12, 31), income=30.0,
    )
    expected_price = modified_dietz(
        1000.0, 1200.0, [(date(2025, 7, 2), 500.0)],
        date(2025, 1, 1), date(2025, 12, 31), income=0.0,
    )
    assert total == pytest.approx(expected_total)
    assert price == pytest.approx(expected_price)
    assert total - price == pytest.approx(divs / (1000.0 + 0.5 * 500.0), abs=1e-6)


# ---------------------------------------------------------------------------
# _close_on_or_before
# ---------------------------------------------------------------------------

def test_close_on_or_before_picks_last_on_or_before():
    h = PriceHistory("AAA", ["2024-06-07", "2024-12-27", "2025-12-26"], [100.0, 120.0, 140.0], "x")
    assert _close_on_or_before(h, date(2024, 12, 31)) == 120.0
    assert _close_on_or_before(h, date(2025, 12, 31)) == 140.0


def test_close_on_or_before_missing_returns_none():
    h = PriceHistory("AAA", ["2024-06-07"], [100.0], "x")
    assert _close_on_or_before(h, date(2023, 12, 31)) is None
    assert _close_on_or_before(None, date(2024, 1, 1)) is None


# ---------------------------------------------------------------------------
# build_performance (integration)
# ---------------------------------------------------------------------------

def _aaa_scenario():
    txns = {
        "AAA": [
            _tx("INIT_BUY", "AAA", date(2024, 6, 1), -1000.0, quantity=10.0, price=100.0),
            _tx("DIVIDEND", "AAA", date(2025, 3, 1), 20.0),
        ]
    }
    histories = {
        "AAA": PriceHistory(
            "AAA",
            ["2024-06-07", "2024-12-27", "2025-12-26", "2026-06-12"],
            [100.0, 120.0, 140.0, 150.0],
            "x",
        )
    }
    shares = {2024: {"AAA": 10.0}, 2025: {"AAA": 10.0}}
    return txns, histories, shares


def test_build_performance_summary_has_symbol_and_portfolio():
    txns, histories, shares = _aaa_scenario()
    summaries, _ = build_performance(
        ["AAA"], txns, {"AAA": 1500.0}, {"AAA": 1000.0}, shares, histories,
        date(2026, 6, 16),
    )
    by_symbol = {s.symbol: s for s in summaries}
    assert set(by_symbol) == {"AAA", PORTFOLIO}
    aaa = by_symbol["AAA"]
    assert aaa.lifetime_total_xirr is not None and aaa.lifetime_total_xirr > 0
    # total return > price return because of the dividend
    assert aaa.lifetime_total_xirr > aaa.lifetime_price_xirr
    assert aaa.income_contribution == pytest.approx(
        aaa.lifetime_total_xirr - aaa.lifetime_price_xirr
    )
    assert by_symbol["AAA"].first_held == date(2024, 6, 1)


def test_build_performance_yearly_rows_chain_values():
    txns, histories, shares = _aaa_scenario()
    _, yearly = build_performance(
        ["AAA"], txns, {"AAA": 1500.0}, {"AAA": 1000.0}, shares, histories,
        date(2026, 6, 16),
    )
    rows = {y.year: y for y in yearly}
    assert set(rows) == {2024, 2025, 2026}
    assert rows[2024].begin_value == 0.0          # not held at start of 2024
    assert rows[2024].end_value == pytest.approx(1200.0)   # 10 * 120
    assert rows[2025].begin_value == pytest.approx(1200.0)  # chains from prior year-end
    assert rows[2025].end_value == pytest.approx(1400.0)    # 10 * 140
    assert rows[2026].end_value == pytest.approx(1500.0)    # current value
    assert all(r.total_return is not None for r in yearly)


def test_build_performance_empty_inputs():
    summaries, yearly = build_performance(
        [], {}, {}, {}, {}, {}, date(2026, 6, 16)
    )
    # only the PORTFOLIO row (no symbols), with None returns
    assert [s.symbol for s in summaries] == [PORTFOLIO]
    assert summaries[0].lifetime_total_xirr is None
    assert yearly == []
