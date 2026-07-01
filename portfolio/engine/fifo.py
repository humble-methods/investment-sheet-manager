"""FIFO lot tracking: replay transactions into open lots, then Positions."""

import logging
from collections import defaultdict, deque

from portfolio.models import Lot, Position, Transaction
from portfolio.parsers.utils import parse_holding_base

logger = logging.getLogger(__name__)

# Tolerance for fractional-share arithmetic (e.g. 30.576 + 50.424 shares).
_EPS = 1e-9

# Tolerance for the broker HOLDING-base cross-check at a split (fractional sh).
_HOLDING_TOLERANCE = 0.01

# Transaction types that create equity lots. INIT_BUY is treated identically
# to BUY for lot building; the distinction is only for source tracking.
_LOT_TYPES = {"BUY", "INIT_BUY"}

# Replay order within a trade_date: INIT_BUY first (bootstrap lots predate
# same-day BUYs), then BUY/SELL, then SPLIT last so a split scales the full
# pre-split position rather than a mid-day partial one.
_TYPE_ORDER = {"INIT_BUY": 0, "SPLIT": 2}


def _sort_key(tx: Transaction) -> tuple:
    """trade_date ascending; INIT_BUY before same-date BUY; SPLIT after both."""
    return (tx.trade_date, _TYPE_ORDER.get(tx.tx_type, 1))


def build_lots(
    transactions: list[Transaction],
) -> dict[tuple[str, str], deque[Lot]]:
    """
    Replay BUY / INIT_BUY / SELL sorted by trade_date ascending into FIFO lots
    keyed by (account_number, symbol).

    BUY / INIT_BUY: append a Lot. SELL (negative quantity): deplete oldest lots
    first. SPLIT (stock split / stock dividend): scale every open lot of the key
    in place (qty x ratio, unit_cost / ratio; acquisition date and total cost
    basis unchanged). All other types (DIVIDEND, INTEREST, CASH_*, ADR_FEE,
    TAX_WITHHOLDING, REINVEST, UNKNOWN) are ignored.

    Raises ValueError if a SELL exceeds the available lots (oversell).
    """
    lots_by_key: dict[tuple[str, str], deque[Lot]] = defaultdict(deque)

    # A split event surfaces as several $0/share SPLIT rows (a +N due bill, a -N
    # reversal, a +N delivery) that NET to the true share change. Pre-aggregate
    # the net delta per (account, symbol) so replay applies one scaling — robust
    # to row order and to the legs spanning files. Also grab the broker's
    # pre-split HOLDING base (first row that carries one) for a sanity check.
    split_delta: dict[tuple[str, str], float] = defaultdict(float)
    split_base: dict[tuple[str, str], float] = {}
    for tx in transactions:
        if tx.tx_type == "SPLIT":
            key = (tx.account_number, tx.symbol)
            split_delta[key] += tx.quantity
            base = parse_holding_base(tx.description)
            if base is not None:
                split_base.setdefault(key, base)
    applied_split: set[tuple[str, str]] = set()

    for tx in sorted(transactions, key=_sort_key):
        if tx.tx_type in _LOT_TYPES:
            key = (tx.account_number, tx.symbol)
            lots_by_key[key].append(
                Lot(tx.account_number, tx.symbol, tx.trade_date, tx.quantity, tx.price)
            )
        elif tx.tx_type == "SELL":
            key = (tx.account_number, tx.symbol)
            dq = lots_by_key[key]
            shares_to_sell = abs(tx.quantity)
            while shares_to_sell > _EPS:
                if not dq:
                    raise ValueError(
                        f"Oversell: SELL of {abs(tx.quantity)} {tx.symbol} in "
                        f"{tx.account_number} on {tx.trade_date} exceeds available lots"
                    )
                lot = dq[0]
                if lot.quantity <= shares_to_sell + _EPS:
                    shares_to_sell -= lot.quantity
                    dq.popleft()
                else:
                    lot.quantity -= shares_to_sell
                    shares_to_sell = 0.0
        elif tx.tx_type == "SPLIT":
            key = (tx.account_number, tx.symbol)
            if key in applied_split:
                continue  # net delta already applied at the first SPLIT leg
            applied_split.add(key)

            dq = lots_by_key.get(key)  # .get: don't auto-vivify an empty deque
            running_qty = sum(lot.quantity for lot in dq) if dq else 0.0
            if running_qty <= _EPS:
                logger.warning(
                    "SPLIT of %s in %s on %s with no open lots; ignored",
                    tx.symbol, tx.account_number, tx.trade_date,
                )
                continue

            base = split_base.get(key)
            if base is not None and abs(base - running_qty) > _HOLDING_TOLERANCE:
                logger.warning(
                    "SPLIT of %s in %s: broker HOLDING base %g != running qty %g "
                    "(possible missed earlier event)",
                    tx.symbol, tx.account_number, base, running_qty,
                )

            ratio = (running_qty + split_delta[key]) / running_qty
            for lot in dq:
                lot.quantity *= ratio
                lot.unit_cost /= ratio

    return lots_by_key


def compute_positions(
    lots_by_key: dict[tuple[str, str], deque[Lot]],
    account_registrations: dict[str, str],
) -> list[Position]:
    """Collapse lot deques into Positions, dropping fully-closed (qty 0) ones."""
    positions: list[Position] = []
    for (account, symbol), dq in lots_by_key.items():
        quantity = sum(lot.quantity for lot in dq)
        if abs(quantity) < _EPS:
            continue
        positions.append(Position(
            account_number=account,
            account_registration=account_registrations.get(account, ""),
            symbol=symbol,
            quantity=quantity,
            lots=list(dq),
        ))
    return positions
