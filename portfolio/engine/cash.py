"""Per-account cash: reconstruct a running balance, reconcile vs snapshot."""

from datetime import date

from portfolio.config import CASH_MMKT_SYMBOL, CASH_SWEEP_CUSIP
from portfolio.engine.holdings import filter_and_partition
from portfolio.models import CashBalance, Transaction

_DEFAULT_TOLERANCE = 0.01

# Symbol-less rows that are NOT real cash movements. A Current Year Contribution
# is booked only for the record — the same dollars already arrive as an IIAXX
# Deposit, so replaying both double-counts (Phase 20, amending Locked Decision 19
# to admit explicit external inflows like Funds Received wires).
_CASH_EXCLUDED_TYPES = {"CONTRIBUTION_INFO"}

# Symbol-less rows whose Amount is signed from the INVESTOR's perspective
# (positive = money IN), the OPPOSITE of the sweep Deposit/Withdrawal ledger rows
# (Merrill's parens-for-inflow convention). Real exports store Bank Interest and
# Funds Received wires as positive credits, so they must be ADDED, not subtracted
# — otherwise a $30,000 wire lands as −$30,000 (an inflow read as an outflow).
_CREDIT_CONVENTION_TYPES = {"INTEREST", "CASH_TRANSFER_IN"}


def cash_account_for(account_registration: str) -> str:
    """Roth accounts settle cash in IIAXX; everything else uses the ML sweep."""
    return CASH_MMKT_SYMBOL if "Roth" in account_registration else CASH_SWEEP_CUSIP


def reconstruct_cash(
    transactions: list[Transaction],
    bootstrap_cash: dict[str, float],
    account_state: dict[str, dict],
) -> dict[str, float]:
    """
    Running cash per account = bootstrap balance + replayed post-cutoff deltas.

    DEFAULT (safe) model: drive cash ONLY from the cash-account rows themselves
    (symbol is None — the 990156937 sweep and IIAXX). Those rows already net the
    economic effect of trades/dividends/fees as they settle into the sweep (see
    Locked Decision 19), so the BUY/SELL/DIVIDEND/ADR_FEE rows — which all carry
    a symbol — are intentionally skipped to avoid double-counting.

        delta = -amount   (Merrill parens convention on the sweep: a Deposit of
                           (19.00) → +19; a Withdrawal of 9,125.00 → -9,125)

    Cutoff + un-bootstrapped skipping reuse filter_and_partition, so only
    post-cutoff rows of bootstrapped accounts contribute.

    SIGN CAVEAT: two opposite conventions coexist. Sweep Deposit/Withdrawal rows
    are parens-for-inflow, so delta = -amount. But Bank Interest (INTEREST) and
    Funds Received wires (CASH_TRANSFER_IN) store a positive amount FOR an inflow,
    so they credit +amount instead (_CREDIT_CONVENTION_TYPES). A wire is the sole
    record of that external cash — no sweep Deposit mirrors it. A "Current Year
    Contribution" (CONTRIBUTION_INFO) is excluded entirely: the same money already
    arrives as an IIAXX Deposit, so counting both double-counts.

    NOTE: pending empirical validation against a real multi-month set. If sweep
    rows turn out NOT to capture every dividend, switch to the economic model
    (sum trade/dividend/fee amounts; treat sweep Deposit/Withdrawal as internal).
    """
    kept, _skipped = filter_and_partition(transactions, account_state)
    balances = dict(bootstrap_cash)

    for tx in kept:
        if tx.symbol is not None:
            continue  # equity/dividend/fee row — already netted into the sweep
        if tx.tx_type in _CASH_EXCLUDED_TYPES:
            continue  # recorded-only row (e.g. Roth contribution double of IIAXX)
        prior = balances.get(tx.account_number, 0.0)
        if tx.tx_type in _CREDIT_CONVENTION_TYPES:
            balances[tx.account_number] = prior + tx.amount  # positive = inflow
        else:
            balances[tx.account_number] = prior - tx.amount  # parens = inflow

    return {account: round(balance, 2) for account, balance in balances.items()}


def reconcile_cash(
    reconstructed: dict[str, float],
    snapshot_cash: dict[str, float] | None,
    as_of_date: date,
    account_registrations: dict[str, str],
    tolerance: float = _DEFAULT_TOLERANCE,
) -> list[CashBalance]:
    """
    Pair reconstructed vs snapshot per account into CashBalance rows (one per
    account, sorted). CashBalance.drift surfaces mismatches beyond `tolerance`
    for the Run Log; snapshot is None when no Holdings CSV arrived this run.

    `account_registrations` (account_number -> registration) is required to fill
    each row's registration and to pick its cash_account (IIAXX vs sweep).
    """
    snapshot_cash = snapshot_cash or {}
    balances: list[CashBalance] = []

    for account in sorted(reconstructed.keys() | snapshot_cash.keys()):
        registration = account_registrations.get(account, "")
        balances.append(CashBalance(
            account_number=account,
            account_registration=registration,
            cash_account=cash_account_for(registration),
            reconstructed=round(reconstructed.get(account, 0.0), 2),
            snapshot=snapshot_cash.get(account),
            as_of_date=as_of_date,
        ))

    return balances
