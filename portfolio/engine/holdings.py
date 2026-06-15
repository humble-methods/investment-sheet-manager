"""Holdings replay: apply cutoff/skip rules, build lots, verify vs snapshot."""

from portfolio.engine.fifo import build_lots, compute_positions
from portfolio.models import Position, Transaction
from portfolio.parsers.utils import parse_date

# Tolerance for fractional-share comparison against Merrill's snapshot.
_DEFAULT_TOLERANCE = 0.01


def filter_and_partition(
    transactions: list[Transaction],
    account_state: dict[str, dict],
) -> tuple[list[Transaction], list[str]]:
    """
    Apply cutoff + skip-and-flag BEFORE lot building.

    - Bootstrapped accounts: drop activity rows with trade_date <= init_date
      (already baked into the snapshot). INIT_BUY rows are exempt — they ARE the
      bootstrap (their acquisition dates predate init_date by design).
    - Un-bootstrapped accounts (no account_state entry): drop ALL their rows and
      return the account_number in the skipped list for the Run Log.

    Returns (kept_transactions, sorted_skipped_account_numbers).
    """
    kept: list[Transaction] = []
    skipped: set[str] = set()

    for tx in transactions:
        state = account_state.get(tx.account_number)
        if state is None:
            skipped.add(tx.account_number)
            continue
        if tx.tx_type == "INIT_BUY":
            kept.append(tx)
            continue
        if tx.trade_date <= parse_date(state["init_date"]):
            continue
        kept.append(tx)

    return kept, sorted(skipped)


def compute_holdings(
    transactions: list[Transaction],
    account_state: dict[str, dict],
) -> tuple[list[Position], list[str]]:
    """
    Full pipeline: filter_and_partition -> build_lots -> compute_positions.
    build_lots handles trade_date sorting (INIT_BUY before same-date BUY).
    Returns (positions, skipped_accounts).
    """
    kept, skipped = filter_and_partition(transactions, account_state)
    registrations = {tx.account_number: tx.account_registration for tx in kept}
    positions = compute_positions(build_lots(kept), registrations)
    return positions, skipped


def verify_against_snapshot(
    positions: list[Position],
    snapshot: dict[tuple[str, str], float],
    tolerance: float = _DEFAULT_TOLERANCE,
) -> list[str]:
    """
    Compare computed quantities against Merrill's Holdings snapshot. Returns one
    "OK ..." or "MISMATCH ..." line per (account, symbol), sorted. Tolerance
    absorbs fractional-share float rounding.
    """
    computed = {(p.account_number, p.symbol): p.quantity for p in positions}
    results: list[str] = []

    for key in sorted(computed.keys() | snapshot.keys()):
        account, symbol = key
        c = computed.get(key, 0.0)
        s = snapshot.get(key, 0.0)
        if abs(c - s) <= tolerance:
            results.append(f"OK {account} {symbol}: {c:g}")
        else:
            results.append(
                f"MISMATCH {account} {symbol}: computed {c:g} vs snapshot {s:g}"
            )

    return results
