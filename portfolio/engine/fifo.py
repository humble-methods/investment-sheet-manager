"""FIFO lot tracking: replay transactions into open lots, then Positions."""

from collections import defaultdict, deque

from portfolio.models import Lot, Position, Transaction

# Tolerance for fractional-share arithmetic (e.g. 30.576 + 50.424 shares).
_EPS = 1e-9

# Transaction types that create equity lots. INIT_BUY is treated identically
# to BUY for lot building; the distinction is only for source tracking.
_LOT_TYPES = {"BUY", "INIT_BUY"}


def _sort_key(tx: Transaction) -> tuple:
    """trade_date ascending; INIT_BUY before a same-date regular BUY."""
    return (tx.trade_date, 0 if tx.tx_type == "INIT_BUY" else 1)


def build_lots(
    transactions: list[Transaction],
) -> dict[tuple[str, str], deque[Lot]]:
    """
    Replay BUY / INIT_BUY / SELL sorted by trade_date ascending into FIFO lots
    keyed by (account_number, symbol).

    BUY / INIT_BUY: append a Lot. SELL (negative quantity): deplete oldest lots
    first. All other types (DIVIDEND, INTEREST, CASH_*, ADR_FEE,
    TAX_WITHHOLDING, REINVEST, UNKNOWN) are ignored.

    Raises ValueError if a SELL exceeds the available lots (oversell).
    """
    lots_by_key: dict[tuple[str, str], deque[Lot]] = defaultdict(deque)

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
