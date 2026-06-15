from pathlib import Path

import pytest

from portfolio.parsers.unrealized_parser import parse_unrealized_csv

SAMPLE = Path(__file__).parent / "sample_data" / "unrealized_sample.csv"


def test_each_lot_becomes_init_buy():
    txns = parse_unrealized_csv(SAMPLE)
    assert len(txns) == 12
    assert all(t.tx_type == "INIT_BUY" for t in txns)
    assert all(t.status == "Settled" for t in txns)
    assert all(t.trade_date == t.settlement_date for t in txns)
    assert all(t.source_file.startswith("INIT:") for t in txns)


def test_iiaxx_row_skipped():
    txns = parse_unrealized_csv(SAMPLE)
    assert not any(t.symbol == "IIAXX" for t in txns)


def test_amount_is_negative_cost_basis():
    txns = parse_unrealized_csv(SAMPLE)
    lot = next(t for t in txns if t.symbol == "AXP" and t.price == 169.72)
    assert lot.amount == pytest.approx(-848.60)
    assert lot.quantity == 5.0
    assert lot.trade_date.isoformat() == "2022-01-04"


def test_symbol_normalization_brkb_to_brk_b():
    txns = parse_unrealized_csv(SAMPLE)
    brkb = [t for t in txns if t.symbol == "BRK-B"]
    assert len(brkb) == 3
    assert all(t.account_number == "11A-00001" for t in brkb)


def test_multiple_lots_same_symbol_different_dates():
    txns = parse_unrealized_csv(SAMPLE)
    axp = [t for t in txns if t.symbol == "AXP"]
    assert len(axp) == 7
    assert {t.trade_date.isoformat() for t in axp} == {"2022-01-04", "2021-11-26", "2021-11-23"}


def test_identical_init_buy_lots_both_kept():
    txns = parse_unrealized_csv(SAMPLE)
    dupes = [
        t for t in txns
        if t.symbol == "AXP" and t.quantity == 5.0 and t.price == 164.35
        and t.trade_date.isoformat() == "2021-11-26"
    ]
    assert len(dupes) == 2


def test_fractional_shares_for_cof():
    txns = parse_unrealized_csv(SAMPLE)
    cof_quantities = {t.quantity for t in txns if t.symbol == "COF"}
    assert cof_quantities == {30.576, 50.424}
