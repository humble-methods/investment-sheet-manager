"""Per-account cash: reconstruct a running balance, reconcile vs snapshot."""

from datetime import date

from portfolio.config import CASH_MMKT_SYMBOL, CASH_SWEEP_CUSIP
from portfolio.engine.holdings import filter_and_partition
from portfolio.models import CashBalance, Transaction
from portfolio.parsers.utils import parse_date

_DEFAULT_TOLERANCE = 0.01


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

    NOTE: pending empirical validation against a real multi-month set. If sweep
    rows turn out NOT to capture every dividend, switch to the economic model
    (sum trade/dividend/fee amounts; treat sweep Deposit/Withdrawal as internal).
    """
    kept, _skipped = filter_and_partition(transactions, account_state)
    balances = dict(bootstrap_cash)

    for tx in kept:
        if tx.symbol is not None:
            continue  # equity/dividend/fee row — already netted into the sweep
        balances[tx.account_number] = balances.get(tx.account_number, 0.0) - tx.amount

    return {account: round(balance, 2) for account, balance in balances.items()}


def cash_balance_series(
    transactions: list[Transaction],
    bootstrap_cash: dict[str, float],
    account_state: dict[str, dict],
) -> dict[str, list[tuple[date, float]]]:
    """Per-account chronological ``[(date, balance)]`` running cash.

    Same sweep-only model as ``reconstruct_cash`` (Decision 19): the opening point is
    each bootstrapped account's init_date at its bootstrap balance, then each
    post-cutoff cash-account row (``symbol is None``) steps the balance by ``-amount``.
    Feeds the time-weighted average idle cash used for opportunity cost. The final
    balance of each series matches ``reconstruct_cash`` for the same inputs.
    """
    kept, _ = filter_and_partition(transactions, account_state)
    series: dict[str, list[tuple[date, float]]] = {
        account: [(parse_date(state["init_date"]), round(bootstrap_cash.get(account, 0.0), 2))]
        for account, state in account_state.items()
    }
    for tx in sorted((t for t in kept if t.symbol is None), key=lambda t: t.trade_date):
        points = series.get(tx.account_number)
        if points is None:
            continue
        points.append((tx.trade_date, round(points[-1][1] - tx.amount, 2)))
    return series


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
