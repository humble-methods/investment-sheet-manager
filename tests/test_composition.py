"""Unit tests for portfolio/metrics/composition.py (pure, offline)."""

from datetime import date

import pytest

from portfolio.metrics.composition import (
    SCOPE_ALL,
    SYMBOL_CASH,
    SYMBOL_OTHER,
    composition_rows,
)
from portfolio.models import CashBalance, Lot, Position

THRESHOLD = 0.015


def _pos(symbol, qty, cost_basis, account="11A-1"):
    """Position with a single lot whose total cost basis == cost_basis."""
    lot = Lot(
        account_number=account,
        symbol=symbol,
        acquisition_date=date(2022, 1, 1),
        quantity=qty,
        unit_cost=cost_basis / qty,
    )
    return Position(
        account_number=account,
        account_registration="CMA-Edge",
        symbol=symbol,
        quantity=qty,
        lots=[lot],
    )


def _cash(account, amount):
    return CashBalance(
        account_number=account,
        account_registration="CMA-Edge",
        cash_account="990156937",
        reconstructed=amount,
        snapshot=None,
        as_of_date=date(2026, 6, 15),
    )


# Scenario shared by most tests:
#   11A-1: AAPL 10 @ cost 1000, close 200 -> mv 2000 ; TINY 1 @ cost 5, close 5 -> mv 5
#   11A-2: MSFT 5 @ cost 1000, close 300 -> mv 1500
#   cash : 11A-1 has 500
POSITIONS = [
    _pos("AAPL", 10, 1000, account="11A-1"),
    _pos("TINY", 1, 5, account="11A-1"),
    _pos("MSFT", 5, 1000, account="11A-2"),
]
LATEST_CLOSE = {"AAPL": 200.0, "TINY": 5.0, "MSFT": 300.0}
CASH = [_cash("11A-1", 500.0)]


def _by_scope(rows, scope):
    return [r for r in rows if r.scope == scope]


def test_weights_sum_to_one_per_scope():
    rows = composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD)
    for scope in {r.scope for r in rows}:
        block = _by_scope(rows, scope)
        assert sum(r.market_weight for r in block) == pytest.approx(1.0)
        assert sum(r.cost_weight for r in block) == pytest.approx(1.0)


def test_other_bucketing_collapses_subthreshold_equity():
    rows = _by_scope(composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD), SCOPE_ALL)
    symbols = [r.symbol for r in rows]
    assert "TINY" not in symbols            # below 1.5% -> bucketed
    other = next(r for r in rows if r.symbol == SYMBOL_OTHER)
    assert other.market_value == pytest.approx(5.0)
    assert other.cost_basis == pytest.approx(5.0)


def test_cash_slice_present_and_never_bucketed():
    rows = _by_scope(composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD), SCOPE_ALL)
    cash_row = next(r for r in rows if r.symbol == SYMBOL_CASH)
    assert cash_row.market_value == pytest.approx(500.0)
    # cash weight = 500 / (2000+5+1500+500)
    assert cash_row.market_weight == pytest.approx(500.0 / 4005.0)


def test_weight_delta_is_market_minus_cost():
    rows = _by_scope(composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD), SCOPE_ALL)
    aapl = next(r for r in rows if r.symbol == "AAPL")
    assert aapl.weight_delta == pytest.approx(aapl.market_weight - aapl.cost_weight)


def test_consolidated_first_and_ordered_by_market_value():
    rows = composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD)
    all_rows = _by_scope(rows, SCOPE_ALL)
    # ALL block leads the output
    assert rows[: len(all_rows)] == all_rows
    # kept equities by market value desc, then Other, then CASH
    assert [r.symbol for r in all_rows] == ["AAPL", "MSFT", SYMBOL_OTHER, SYMBOL_CASH]


def test_per_account_scope_no_cash_has_no_cash_row():
    rows = _by_scope(composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD), "11A-2")
    assert [r.symbol for r in rows] == ["MSFT"]
    assert rows[0].market_weight == pytest.approx(1.0)
    assert rows[0].cost_weight == pytest.approx(1.0)


def test_per_account_scopes_emitted_in_account_order():
    rows = composition_rows(POSITIONS, LATEST_CLOSE, CASH, THRESHOLD)
    scopes = [r.scope for r in rows]
    assert scopes[0] == SCOPE_ALL
    # first appearance of each account scope, in sorted account order
    first_seen = list(dict.fromkeys(s for s in scopes if s != SCOPE_ALL))
    assert first_seen == ["11A-1", "11A-2"]


def test_missing_price_yields_zero_market_value_no_crash():
    rows = _by_scope(
        composition_rows([_pos("NOPRICE", 1, 100)], {}, [], THRESHOLD), SCOPE_ALL
    )
    # zero market value -> below threshold -> Other; cost side still meaningful
    other = next(r for r in rows if r.symbol == SYMBOL_OTHER)
    assert other.market_value == 0.0
    assert other.cost_weight == pytest.approx(1.0)


def test_empty_inputs_produce_no_rows():
    assert composition_rows([], {}, [], THRESHOLD) == []
