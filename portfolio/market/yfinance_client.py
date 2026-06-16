"""yfinance wrapper: fetch fundamentals with a Drive-backed JSON cache (TTL).

Only fundamental data (P/E, dividend yield, ROE history, net income, book
value) is fetched here. Prices stay in Google Sheets via GOOGFINANCE — Python
never fetches them.
"""

import dataclasses
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from portfolio.config import CACHE_TTL_HOURS
from portfolio.market.symbol_overrides import normalize_all
from portfolio.metrics.fundamentals import roe_history

logger = logging.getLogger(__name__)


@dataclass
class StockFundamentals:
    symbol: str                # normalized (yfinance) symbol
    pe_ratio: float | None
    dividend_yield: float | None  # yfinance raw value (a fraction, e.g. 0.0055)
    roe_current: float | None  # info["returnOnEquity"] (trailing 12mo)
    roe_1y: float | None       # most recent fiscal year (from statements)
    roe_2y: float | None
    roe_3y: float | None
    roe_4y: float | None
    net_income: float | None   # latest annual Net Income
    book_value: float | None   # latest annual Stockholders Equity
    fetched_at: str            # ISO datetime string


_CACHE_FIELDS = tuple(f.name for f in dataclasses.fields(StockFundamentals))


def _ticker(symbol: str):
    """yfinance Ticker for ``symbol``. Isolated so tests can monkeypatch it.

    yfinance is imported lazily so importing this module (and the offline test
    suite) doesn't pull in the network library.
    """
    import yfinance as yf

    return yf.Ticker(symbol)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _is_fresh(entry: dict, ttl_hours: int) -> bool:
    fetched_at = entry.get("fetched_at")
    if not fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    return datetime.now() - fetched < timedelta(hours=ttl_hours)


def _from_cache(entry: dict) -> StockFundamentals:
    """Rebuild from a cache dict, tolerating missing/extra keys."""
    return StockFundamentals(**{name: entry.get(name) for name in _CACHE_FIELDS})


def _empty(symbol: str) -> StockFundamentals:
    """All-None record for a symbol we couldn't fetch and have no cache for."""
    return StockFundamentals(
        symbol=symbol,
        pe_ratio=None,
        dividend_yield=None,
        roe_current=None,
        roe_1y=None,
        roe_2y=None,
        roe_3y=None,
        roe_4y=None,
        net_income=None,
        book_value=None,
        fetched_at=_now_iso(),
    )


def _fetch_one(symbol: str) -> StockFundamentals:
    """Pull one symbol's fundamentals from yfinance. May raise on network error."""
    ticker = _ticker(symbol)
    info = ticker.info or {}
    net_income, book_value, roes = roe_history(ticker.financials, ticker.balance_sheet)
    roe_1y, roe_2y, roe_3y, roe_4y = (roes + [None, None, None, None])[:4]
    return StockFundamentals(
        symbol=symbol,
        pe_ratio=info.get("trailingPE"),
        dividend_yield=info.get("dividendYield"),
        roe_current=info.get("returnOnEquity"),
        roe_1y=roe_1y,
        roe_2y=roe_2y,
        roe_3y=roe_3y,
        roe_4y=roe_4y,
        net_income=net_income,
        book_value=book_value,
        fetched_at=_now_iso(),
    )


def fetch_fundamentals(
    symbols: Iterable[str],
    cache: dict,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> dict[str, StockFundamentals]:
    """Fundamentals per symbol, cache-first.

    For each (normalized, de-duped) symbol:
      - fresh cache entry (< ttl_hours old) -> use it, no network call
      - otherwise fetch from yfinance and refresh the cache entry
      - on fetch failure -> keep the stale cache entry if present, else return a
        None-filled record (NOT cached, so the next run retries)

    ``cache`` is mutated in place (the caller persists it to Drive). Returns
    ``{symbol: StockFundamentals}`` keyed by normalized symbol.
    """
    results: dict[str, StockFundamentals] = {}

    for symbol in normalize_all(symbols):
        entry = cache.get(symbol)
        if entry is not None and _is_fresh(entry, ttl_hours):
            results[symbol] = _from_cache(entry)
            continue
        try:
            fundamentals = _fetch_one(symbol)
        except Exception as exc:  # yfinance raises a wide range of errors
            logger.warning("yfinance fetch failed for %s: %s", symbol, exc)
            results[symbol] = _from_cache(entry) if entry is not None else _empty(symbol)
            continue
        cache[symbol] = dataclasses.asdict(fundamentals)
        results[symbol] = fundamentals

    return results
