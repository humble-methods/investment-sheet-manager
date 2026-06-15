from datetime import date

from portfolio.engine.holdings import (
    compute_holdings,
    filter_and_partition,
    verify_against_snapshot,
)
from portfolio.models import Position, Transaction

# Both accounts bootstrapped with a 1/30/2026 init/cutoff date.
STATE = {
    "11A-00003": {"init_date": "1/30/2026", "bootstrap_source_file": "Unrealized.csv"},
    "11A-00001": {"init_date": "1/30/2026", "bootstrap_source_file": "Unrealized.csv"},
}


def make_tx(tx_type, symbol, quantity, d, *, account="11A-00003"):
    return Transaction(
        trade_date=d, settlement_date=d, status="Settled",
        account_number=account, account_registration="CMA-Edge",
        tx_type=tx_type, description="", symbol=symbol,
        quantity=quantity, price=100.0, amount=0.0, source_file="t.csv",
    )


def test_activity_on_or_before_init_date_is_dropped():
    txns = [
        make_tx("BUY", "X", 5, date(2026, 1, 30)),   # == init_date -> dropped
        make_tx("BUY", "X", 5, date(2026, 1, 15)),   # < init_date  -> dropped
    ]
    kept, skipped = filter_and_partition(txns, STATE)
    assert kept == []
    assert skipped == []


def test_activity_after_init_date_is_kept():
    txns = [make_tx("BUY", "X", 5, date(2026, 2, 1))]
    kept, _ = filter_and_partition(txns, STATE)
    assert len(kept) == 1


def test_init_buy_exempt_from_cutoff():
    # INIT_BUY acquisition dates predate the cutoff but must survive.
    txns = [make_tx("INIT_BUY", "AXP", 5, date(2021, 11, 26))]
    kept, _ = filter_and_partition(txns, STATE)
    assert len(kept) == 1


def test_unbootstrapped_account_all_rows_dropped_and_flagged():
    txns = [
        make_tx("BUY", "X", 5, date(2026, 2, 1), account="99Z-99999"),
        make_tx("INIT_BUY", "X", 5, date(2021, 1, 1), account="99Z-99999"),
    ]
    kept, skipped = filter_and_partition(txns, STATE)
    assert kept == []
    assert skipped == ["99Z-99999"]


def test_mixed_run_bootstrapped_processes_unbootstrapped_skipped():
    txns = [
        make_tx("INIT_BUY", "X", 10, date(2022, 1, 1), account="11A-00003"),
        make_tx("BUY", "Y", 7, date(2026, 2, 1), account="99Z-99999"),
    ]
    positions, skipped = compute_holdings(txns, STATE)
    assert skipped == ["99Z-99999"]
    assert {p.symbol for p in positions} == {"X"}
    assert positions[0].quantity == 10


def test_compute_holdings_replays_init_plus_activity():
    txns = [
        make_tx("INIT_BUY", "X", 10, date(2022, 1, 1)),
        make_tx("BUY", "X", 5, date(2026, 2, 1)),
        make_tx("SELL", "X", -3, date(2026, 3, 1)),
    ]
    positions, _ = compute_holdings(txns, STATE)
    assert len(positions) == 1
    assert positions[0].quantity == 12


def test_verify_against_snapshot_ok_and_mismatch():
    positions = [
        Position("11A-00003", "CMA-Edge", "X", 10.0),
        Position("11A-00003", "CMA-Edge", "Y", 5.0),
    ]
    snapshot = {
        ("11A-00003", "X"): 10.0,     # OK
        ("11A-00003", "Y"): 8.0,      # MISMATCH (5 vs 8)
        ("11A-00003", "Z"): 3.0,      # MISMATCH (held nothing computed)
    }
    lines = verify_against_snapshot(positions, snapshot)
    joined = "\n".join(lines)
    assert "OK 11A-00003 X" in joined
    assert "MISMATCH 11A-00003 Y" in joined
    assert "MISMATCH 11A-00003 Z" in joined


def test_verify_tolerance_absorbs_fractional_rounding():
    positions = [Position("11A-00003", "CMA-Edge", "COF", 81.0000004)]
    lines = verify_against_snapshot(positions, {("11A-00003", "COF"): 81.0})
    assert lines == ["OK 11A-00003 COF: 81"]
