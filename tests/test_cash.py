from datetime import date

from portfolio.engine.cash import (
    cash_account_for,
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
    # Bank Interest breaks the sweep parens convention: real exports ship the
    # credit POSITIVE (e.g. "2.00"), so it must be ADDED, not subtracted.
    txns = [cash_tx("INTEREST", 2.0)]
    balances = reconstruct_cash(txns, {"11A-00003": 100.0}, STATE)
    assert balances["11A-00003"] == 102.0


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


def test_funds_received_wire_credits_cash():
    # Phase 20 (sign-fix): a "Funds Received" wire is CASH_TRANSFER_IN and its
    # Amount ships POSITIVE for an inflow (real: "30,000.00", no parens). It must
    # ADD to cash — the bug this guards is a $30k wire landing as -$30k.
    txns = [cash_tx("CASH_TRANSFER_IN", 30000.0)]
    balances = reconstruct_cash(txns, {"11A-00003": 0.0}, STATE)
    assert balances["11A-00003"] == 30000.0


def test_funds_received_not_subtracted_like_sweep_deposit():
    # Regression for the observed drift: treating the wire like a parens-signed
    # sweep Deposit (delta = -amount) would flip +30k to -30k. Guard the sign.
    txns = [cash_tx("CASH_TRANSFER_IN", 30000.0)]
    balances = reconstruct_cash(txns, {"11A-00003": 5000.0}, STATE)
    assert balances["11A-00003"] == 35000.0  # NOT -25000


def test_contribution_info_excluded_no_double_count():
    # Phase 20: a Current Year Contribution is recorded-only; the same $8,000
    # already arrives as an IIAXX Deposit, so only the deposit moves cash. The
    # contribution ships positive; the IIAXX sweep deposit ships parenthesized.
    txns = [
        cash_tx("CONTRIBUTION_INFO", 8000.0, account="22B-00001",
                registration="Roth IRA-Edge"),
        cash_tx("CASH_IN", -8000.0, account="22B-00001",
                registration="Roth IRA-Edge"),  # the IIAXX deposit (parens)
    ]
    balances = reconstruct_cash(txns, {"22B-00001": 0.0}, STATE)
    assert balances["22B-00001"] == 8000.0  # counted once, not 16000


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
