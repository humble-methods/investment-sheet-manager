"""Portfolio composition: market-value weight vs cost-basis weight per symbol.

Pure computation — prices and balances are passed in. Produces one block of rows
per scope (``"ALL"`` consolidated + each ``account_number``): each equity symbol's
market weight, cost weight, and the delta between them, with small positions rolled
into a single ``"Other"`` slice and cash as its own (never-bucketed) slice.

Market values use the latest weekly close from the run's ``PriceHistory`` (passed in
as ``latest_close``); cost basis comes from the FIFO ``Position`` lots. The delta
(market_weight − cost_weight) is the signal: a symbol that has grown into a larger
share than the capital deployed in it (positive delta) is a concentration/trim
candidate; a laggard shows a negative delta.
"""

from __future__ import annotations

from dataclasses import dataclass

from portfolio.models import CashBalance, Position

SCOPE_ALL = "ALL"
SYMBOL_CASH = "CASH"
SYMBOL_OTHER = "Other"


@dataclass
class CompositionRow:
    scope: str            # "ALL" (consolidated) or an account_number
    symbol: str           # ticker, "Other" (bucketed tail), or "CASH"
    market_value: float
    market_weight: float  # fraction of scope market value (0..1)
    cost_basis: float
    cost_weight: float    # fraction of scope cost basis (0..1)
    weight_delta: float   # market_weight − cost_weight


def _weight(value: float, total: float) -> float:
    return value / total if total else 0.0


def _scope_rows(
    scope: str,
    equities: dict[str, tuple[float, float]],  # symbol -> (market_value, cost_basis)
    cash_value: float,
    threshold: float,
) -> list[CompositionRow]:
    """Build the ordered rows for a single scope.

    Equities whose market weight is below ``threshold`` collapse into one ``Other``
    row; cash (if non-zero) is always its own slice. Weights are normalized within
    the scope, so the market column and the cost column each sum to ~1.0. Order:
    kept equities by market value desc, then ``Other``, then ``CASH``.
    """
    total_mv = sum(mv for mv, _ in equities.values()) + cash_value
    total_cb = sum(cb for _, cb in equities.values()) + cash_value

    def _row(symbol: str, mv: float, cb: float) -> CompositionRow:
        mw, cw = _weight(mv, total_mv), _weight(cb, total_cb)
        return CompositionRow(scope, symbol, mv, mw, cb, cw, mw - cw)

    kept: dict[str, tuple[float, float]] = {}
    other_mv = other_cb = 0.0
    for symbol, (mv, cb) in equities.items():
        if _weight(mv, total_mv) < threshold:
            other_mv += mv
            other_cb += cb
        else:
            kept[symbol] = (mv, cb)

    rows = [
        _row(symbol, mv, cb)
        for symbol, (mv, cb) in sorted(
            kept.items(), key=lambda kv: kv[1][0], reverse=True
        )
    ]
    if other_mv or other_cb:
        rows.append(_row(SYMBOL_OTHER, other_mv, other_cb))
    if cash_value:
        rows.append(_row(SYMBOL_CASH, cash_value, cash_value))
    return rows


def composition_rows(
    positions: list[Position],
    latest_close: dict[str, float],
    cash_balances: list[CashBalance],
    threshold: float,
) -> list[CompositionRow]:
    """Composition rows for the consolidated portfolio plus each account.

    ``positions`` carry symbol + quantity + cost basis; ``latest_close`` maps each
    (normalized) symbol to its most recent close; ``cash_balances`` supply the cash
    slice (reconstructed balance is treated as both market value and cost). A symbol
    missing from ``latest_close`` contributes market value 0 (a visible data gap)
    while still appearing via its cost basis.

    Emits the ``"ALL"`` scope first (contiguous, for an easy single-range pie chart),
    then one block per account in account-number order.
    """
    accounts = sorted(
        {p.account_number for p in positions}
        | {b.account_number for b in cash_balances}
    )

    def equities_for(account: str | None) -> dict[str, tuple[float, float]]:
        agg: dict[str, list[float]] = {}
        for p in positions:
            if account is not None and p.account_number != account:
                continue
            mv = p.quantity * (latest_close.get(p.symbol) or 0.0)
            cur = agg.setdefault(p.symbol, [0.0, 0.0])
            cur[0] += mv
            cur[1] += p.total_cost_basis
        return {symbol: (mv, cb) for symbol, (mv, cb) in agg.items()}

    def cash_for(account: str | None) -> float:
        return sum(
            b.reconstructed
            for b in cash_balances
            if account is None or b.account_number == account
        )

    rows = _scope_rows(SCOPE_ALL, equities_for(None), cash_for(None), threshold)
    for account in accounts:
        rows.extend(
            _scope_rows(account, equities_for(account), cash_for(account), threshold)
        )
    return rows
