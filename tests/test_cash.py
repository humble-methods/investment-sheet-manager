from datetime import date

from portfolio.engine.cash import (
    cash_account_for,
    cash_balance_series,
    reconcile_cash,
    reconstruct_cash,
)
from portfolio.models import Transaction

STATE = {
    "11A-00003": {"init_date": "1/1/2020", "bootstrap_source_file": "U.csv"},
    "22B-00001": {"init_date": "1/1/2020", "bootstrap_source_file": "U.csv"},
}
REGS = {"11A-00003": "CMA-Edge", "22B-00001": "Roth IRA-Edge"}


def cash_tx(tx_type, amount, *, account="11A-00003", registration="CMA-Edge", symbol=None):
    d = date(2026, 2, 1)
    return Transaction(
        trade_date=d, settlement_date=d, status="Settled",
        account_number=account, account_registration=registration,
        tx_type=tx_type, description="", symbol=symbol,
        quantity=None, price=None, amount=amount, source_file="t.csv",
    )


def test_deposit_and_withdrawal_deltas():
    # Deposit "(19.00)" parses to amount -19  -> +19 cash.
    # Withdrawal "9,125.00" parses to amount 9125 -> -9,125 cash.
    txns = [
        cash_tx("CASH_IN", -19.0),
        cash_tx("CASH_OUT", 9125.0),
    ]
    balances = reconstruct_cash(txns, {"11A-00003": 1000.0}, STATE)
    assert balances["11A-00003"] == 1000.0 + 19.0 - 9125.0


def test_bank_interest_credits_cash():
    # Sweep Bank Interest follows the same parens convention: a credit ships
    # negative, so delta = -amount adds to cash. (Pending multi-month validation.)
    txns = [cash_tx("INTEREST", -3.0)]
    balances = reconstruct_cash(txns, {"11A-00003": 100.0}, STATE)
    assert balances["11A-00003"] == 103.0


def test_equity_rows_do_not_touch_cash():
    # BUY/SELL/DIVIDEND carry a symbol and are already netted into the sweep
    # settlement rows; counting them here would double-count.
    txns = [
        cash_tx("BUY", -3211.38, symbol="CSCO"),
        cash_tx("DIVIDEND", 41.20, symbol="MCO"),
        cash_tx("CASH_IN", -41412.0),  # the actual sweep deposit
    ]
    balances = reconstruct_cash(txns, {"11A-00003": 0.0}, STATE)
    assert balances["11A-00003"] == 41412.0


def test_pre_cutoff_and_unbootstrapped_rows_excluded():
    txns = [
        cash_tx("CASH_IN", -100.0, account="11A-00003"),                       # kept
        Transaction(  # pre-cutoff (<= init_date) -> dropped
            trade_date=date(2019, 1, 1), settlement_date=date(2019, 1, 1),
            status="Settled", account_number="11A-00003",
            account_registration="CMA-Edge", tx_type="CASH_IN", description="",
            symbol=None, quantity=None, price=None, amount=-500.0, source_file="t.csv",
        ),
        cash_tx("CASH_IN", -777.0, account="99Z-99999"),                       # unbootstrapped
    ]
    balances = reconstruct_cash(txns, {"11A-00003": 0.0}, STATE)
    assert balances["11A-00003"] == 100.0
    assert "99Z-99999" not in balances


def test_cash_balance_series_opening_and_steps():
    txns = [cash_tx("CASH_IN", -100.0), cash_tx("CASH_OUT", 30.0)]
    series = cash_balance_series(txns, {"11A-00003": 1000.0}, STATE)
    pts = series["11A-00003"]
    # opening point at init_date with the bootstrap balance
    assert pts[0] == (date(2020, 1, 1), 1000.0)
    # two cash rows step the balance: +100 then -30
    assert pts[-1][1] == 1000.0 + 100.0 - 30.0
    # final balance matches reconstruct_cash for the same inputs
    assert pts[-1][1] == reconstruct_cash(txns, {"11A-00003": 1000.0}, STATE)["11A-00003"]


def test_cash_balance_series_equity_rows_excluded():
    txns = [cash_tx("BUY", -500.0, symbol="CSCO"), cash_tx("CASH_IN", -100.0)]
    series = cash_balance_series(txns, {"11A-00003": 0.0}, STATE)
    # only the cash row (symbol None) moves the balance
    assert series["11A-00003"][-1][1] == 100.0


def test_reconcile_drift_none_without_snapshot():
    balances = reconcile_cash({"11A-00003": 100.0}, None, date(2026, 2, 1), REGS)
    assert len(balances) == 1
    assert balances[0].snapshot is None
    assert balances[0].drift is None


def test_reconcile_drift_computed_with_snapshot():
    balances = reconcile_cash(
        {"11A-00003": 100.0}, {"11A-00003": 95.0}, date(2026, 2, 1), REGS
    )
    assert balances[0].snapshot == 95.0
    assert balances[0].drift == 5.0


def test_cash_account_label_by_registration():
    assert cash_account_for("Roth IRA-Edge") == "IIAXX"
    assert cash_account_for("CMA-Edge") == "990156937"


def test_reconcile_assigns_cash_account_per_account():
    balances = reconcile_cash(
        {"11A-00003": 100.0, "22B-00001": 50.0},
        None, date(2026, 2, 1), REGS,
    )
    by_acct = {b.account_number: b.cash_account for b in balances}
    assert by_acct == {"11A-00003": "990156937", "22B-00001": "IIAXX"}
