from datetime import date

import pytest

from portfolio.engine.fifo import build_lots, compute_positions
from portfolio.models import Transaction


def make_tx(tx_type, symbol, quantity, day, *, account="11A-00003", price=100.0):
    d = date(2024, 1, day)
    return Transaction(
        trade_date=d, settlement_date=d, status="Settled",
        account_number=account, account_registration="CMA-Edge",
        tx_type=tx_type, description="", symbol=symbol,
        quantity=quantity, price=price, amount=0.0, source_file="t.csv",
    )


def qty(lots_by_key, account, symbol):
    return sum(lot.quantity for lot in lots_by_key[(account, symbol)])


def test_three_buys_partial_sell_remaining_lots():
    txns = [
        make_tx("BUY", "X", 10, 1, price=10.0),
        make_tx("BUY", "X", 20, 2, price=20.0),
        make_tx("BUY", "X", 30, 3, price=30.0),
        make_tx("SELL", "X", -15, 4),
    ]
    lots = build_lots(txns)
    dq = lots[("11A-00003", "X")]
    # First 10-share lot fully consumed, second lot reduced 20 -> 15.
    assert [lot.quantity for lot in dq] == [15, 30]
    assert qty(lots, "11A-00003", "X") == 45


def test_fifo_oldest_lot_depleted_first():
    txns = [
        make_tx("BUY", "X", 10, 1, price=10.0),
        make_tx("BUY", "X", 10, 2, price=99.0),
        make_tx("SELL", "X", -10, 3),
    ]
    dq = build_lots(txns)[("11A-00003", "X")]
    # The $10 lot (oldest) is gone; the $99 lot survives.
    assert len(dq) == 1
    assert dq[0].unit_cost == 99.0


def test_fractional_shares():
    txns = [
        make_tx("BUY", "COF", 30.576, 1),
        make_tx("BUY", "COF", 50.424, 2),
        make_tx("SELL", "COF", -10.0, 3),
    ]
    assert qty(build_lots(txns), "11A-00003", "COF") == pytest.approx(71.0)


def test_fractional_shares_full_liquidation_drops_position():
    txns = [
        make_tx("BUY", "COF", 30.576, 1),
        make_tx("BUY", "COF", 50.424, 2),
        make_tx("SELL", "COF", -81.0, 3),
    ]
    positions = compute_positions(build_lots(txns), {"11A-00003": "CMA-Edge"})
    assert positions == []


def test_init_buy_treated_like_buy():
    txns = [
        make_tx("INIT_BUY", "X", 5, 1),
        make_tx("BUY", "X", 5, 2),
    ]
    assert qty(build_lots(txns), "11A-00003", "X") == 10


def test_multiple_init_buys_sort_before_regular_buy_same_date():
    # A regular BUY listed first but dated same day as INIT_BUY must still come
    # AFTER the INIT_BUY in FIFO order, so the sell hits the INIT_BUY lot first.
    txns = [
        make_tx("BUY", "X", 5, 5, price=50.0),
        make_tx("INIT_BUY", "X", 5, 5, price=10.0),
        make_tx("SELL", "X", -5, 6),
    ]
    dq = build_lots(txns)[("11A-00003", "X")]
    assert len(dq) == 1
    assert dq[0].unit_cost == 50.0  # INIT_BUY lot ($10) sold first


def test_identical_init_buy_lots_both_kept():
    # AXP-style duplicate lots: two identical 5 sh @ 164.35 are both real.
    txns = [
        make_tx("INIT_BUY", "AXP", 5, 1, price=164.35),
        make_tx("INIT_BUY", "AXP", 5, 1, price=164.35),
    ]
    dq = build_lots(txns)[("11A-00003", "AXP")]
    assert len(dq) == 2
    assert qty(build_lots(txns), "11A-00003", "AXP") == 10


def test_oversell_raises_value_error():
    txns = [
        make_tx("BUY", "X", 5, 1),
        make_tx("SELL", "X", -10, 2),
    ]
    with pytest.raises(ValueError, match="Oversell"):
        build_lots(txns)


def test_renamed_ticker_sell_depletes_bootstrap_lots():
    # Regression: ATGE (Adtalem) renamed to CVSA (Covista). Bootstrap lots arrive
    # under the OLD ticker; the later full SELL arrives under the NEW ticker. Both
    # must normalize to one symbol or the SELL oversells (the reported crash:
    # "Oversell: SELL of 84.0 CVSA ... exceeds available lots").
    from portfolio.parsers.utils import clean_symbol

    txns = [
        make_tx("INIT_BUY", clean_symbol("ATGE"), 80, 1, price=29.20),
        make_tx("INIT_BUY", clean_symbol("ATGE"), 4, 1, price=126.80),
        make_tx("SELL", clean_symbol("CVSA"), -84, 2, price=124.11),
    ]
    positions = compute_positions(build_lots(txns), {"11A-00003": "CMA-Edge"})
    assert positions == []  # 84 sold against 84 bootstrap shares; no oversell


def test_dividend_is_ignored():
    txns = [
        make_tx("BUY", "X", 10, 1),
        make_tx("DIVIDEND", "X", None, 2),
    ]
    assert qty(build_lots(txns), "11A-00003", "X") == 10


def test_positions_carry_registration_and_drop_zero():
    txns = [
        make_tx("BUY", "X", 10, 1),
        make_tx("BUY", "Y", 5, 1, account="22B-00001"),
        make_tx("SELL", "Y", -5, 2, account="22B-00001"),
    ]
    regs = {"11A-00003": "CMA-Edge", "22B-00001": "Roth IRA-Edge"}
    positions = compute_positions(build_lots(txns), regs)
    assert len(positions) == 1
    assert positions[0].symbol == "X"
    assert positions[0].account_registration == "CMA-Edge"
