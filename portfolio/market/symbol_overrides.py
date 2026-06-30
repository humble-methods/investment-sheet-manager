"""Ticker normalization (Merrill -> yfinance) and cash-position detection.

Per CLAUDE.md this is the canonical home for symbol normalization. The override
*data* lives in config (SYMBOL_OVERRIDES / CASH_* sets); this module is the
function layer everything else calls — parsers via clean_symbol, the market
layer directly.
"""

from collections.abc import Iterable

from portfolio.config import (
    CASH_CUSIPS,
    CASH_SYMBOLS,
    SYMBOL_OVERRIDES,
    TICKER_RENAMES,
)


def normalize_symbol(raw: str) -> str:
    """Merrill ticker -> canonical yfinance ticker (e.g. "BRKB" -> "BRK-B").

    Two-stage: first apply a ticker RENAME (old ticker -> current ticker, for
    corporate actions like ATGE -> CVSA) so bootstrap lots and later activity
    unify; then apply the Merrill-vs-Yahoo SPELLING override. Idempotent —
    already-normalized symbols pass through unchanged, so it is safe to call on
    held-position symbols that were normalized at parse time.
    """
    raw = raw.strip()
    action = TICKER_RENAMES.get(raw)
    if action is not None:
        raw = action.new_symbol
    return SYMBOL_OVERRIDES.get(raw, raw)


def is_cash(symbol: str, cusip: str = "") -> bool:
    """True for non-equity cash / money-market positions (ML sweep, IIAXX).

    These never get a yfinance lookup or an equity lot.
    """
    symbol = symbol.strip()
    cusip = cusip.strip()
    return symbol in CASH_SYMBOLS or symbol in CASH_CUSIPS or cusip in CASH_CUSIPS


def normalize_all(symbols: Iterable[str]) -> list[str]:
    """Normalize an iterable of raw symbols, dropping cash/blank and de-duping.

    Order-preserving (first occurrence wins) so callers get stable output. Safe
    to feed straight into fetch_fundamentals.
    """
    seen: dict[str, None] = {}
    for raw in symbols:
        if not raw or raw.strip() in ("", "--"):
            continue
        if is_cash(raw):
            continue
        seen.setdefault(normalize_symbol(raw), None)
    return list(seen)
