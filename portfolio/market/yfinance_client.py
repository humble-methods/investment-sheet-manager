"""yfinance wrapper: fetch fundamentals + price history with Drive-backed JSON caches (TTL).

Fundamental data (P/E, dividend yield, ROE history, net income, book value) and
5-year weekly closing-price history are fetched here. The *current* price stays
in Google Sheets via GOOGLEFINANCE — Python only fetches the historical series,
not live quotes.
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
    # ROE keyed by fiscal period-end CALENDAR YEAR (string keys for JSON), e.g.
    # {"2025": 0.16, "2024": 0.14}. yfinance yields ~4 years per fetch; this dict
    # ACCUMULATES forward across runs (fetch_fundamentals merges), so it can grow
    # toward the 10 years the sheet is provisioned for. Older years persist even
    # after they drop out of yfinance's window.
    roe_by_year: dict
    net_income: float | None   # latest annual Net Income
    book_value: float | None   # latest annual Stockholders Equity
    fetched_at: str            # ISO datetime string


_CACHE_FIELDS = tuple(f.name for f in dataclasses.fields(StockFundamentals))


@dataclass
class PriceHistory:
    symbol: str           # normalized (yfinance) symbol
    dates: list[str]      # ISO "YYYY-MM-DD", oldest→newest
    closes: list[float]   # weekly closing prices, parallel to dates
    fetched_at: str       # ISO datetime string


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
    data = {name: entry.get(name) for name in _CACHE_FIELDS}
    if not isinstance(data.get("roe_by_year"), dict):
        data["roe_by_year"] = {}
    return StockFundamentals(**data)


def _empty(symbol: str) -> StockFundamentals:
    """All-None record for a symbol we couldn't fetch and have no cache for."""
    return StockFundamentals(
        symbol=symbol,
        pe_ratio=None,
        dividend_yield=None,
        roe_current=None,
        roe_by_year={},
        net_income=None,
        book_value=None,
        fetched_at=_now_iso(),
    )


def is_empty_fundamentals(f: StockFundamentals) -> bool:
    """True when yfinance returned nothing usable for this symbol.

    Lets the runner surface symbols that came back blank (e.g. a brand-new spinoff
    ADR not yet indexed by Yahoo) into the Run Log, instead of silently writing an
    all-empty Stock Metrics row.
    """
    return (
        f.pe_ratio is None
        and f.dividend_yield is None
        and f.roe_current is None
        and f.net_income is None
        and f.book_value is None
        and not f.roe_by_year
    )


def _fetch_one(symbol: str) -> StockFundamentals:
    """Pull one symbol's fundamentals from yfinance. May raise on network error."""
    ticker = _ticker(symbol)
    info = ticker.info or {}
    net_income, book_value, roe_by_year = roe_history(
        ticker.financials, ticker.balance_sheet
    )
    return StockFundamentals(
        symbol=symbol,
        pe_ratio=info.get("trailingPE"),
        dividend_yield=info.get("dividendYield"),
        roe_current=info.get("returnOnEquity"),
        roe_by_year={str(year): value for year, value in roe_by_year.items()},
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
        # Accumulate ROE history by calendar year: prior cached years persist even
        # after they fall out of yfinance's ~4-year window; freshly fetched years
        # win on collision. This is how the cache grows toward the sheet's 10 years.
        prior = entry.get("roe_by_year") if isinstance(entry, dict) else None
        if isinstance(prior, dict):
            fundamentals.roe_by_year = {**prior, **fundamentals.roe_by_year}
        cache[symbol] = dataclasses.asdict(fundamentals)
        results[symbol] = fundamentals

    return results


# ---------------------------------------------------------------------------
# Price history (5-year weekly closes)
# ---------------------------------------------------------------------------


def _index_to_iso(ts) -> str:
    """pandas Timestamp / datetime index label → ISO 'YYYY-MM-DD'."""
    if hasattr(ts, "date"):
        return ts.date().isoformat()
    return str(ts)[:10]


def _price_history_from_cache(entry: dict) -> PriceHistory:
    """Rebuild from a cache dict, tolerating missing keys."""
    return PriceHistory(
        symbol=entry.get("symbol", ""),
        dates=list(entry.get("dates") or []),
        closes=list(entry.get("closes") or []),
        fetched_at=entry.get("fetched_at", ""),
    )


def _empty_history(symbol: str) -> PriceHistory:
    """Empty record for a symbol we couldn't fetch and have no cache for."""
    return PriceHistory(symbol=symbol, dates=[], closes=[], fetched_at=_now_iso())


def _fetch_history_one(symbol: str, period: str, interval: str) -> PriceHistory:
    """Pull one symbol's closing-price history from yfinance. May raise on network error."""
    ticker = _ticker(symbol)
    hist = ticker.history(period=period, interval=interval, auto_adjust=False)
    dates: list[str] = []
    closes: list[float] = []
    if hist is not None and not getattr(hist, "empty", True):
        for idx, value in hist["Close"].items():
            if value is None or value != value:  # skip NaN (value != value)
                continue
            dates.append(_index_to_iso(idx))
            closes.append(float(value))
    return PriceHistory(symbol=symbol, dates=dates, closes=closes, fetched_at=_now_iso())


def fetch_price_history(
    symbols: Iterable[str],
    cache: dict,
    ttl_hours: int = CACHE_TTL_HOURS,
    period: str = "5y",
    interval: str = "1wk",
) -> dict[str, PriceHistory]:
    """5-year weekly closing prices per symbol, cache-first.

    Same control flow as ``fetch_fundamentals``:
      - fresh cache entry (< ttl_hours old) -> use it, no network call
      - otherwise fetch from yfinance and refresh the cache entry
      - on fetch failure -> keep the stale entry if present, else return an empty
        record (NOT cached, so the next run retries)

    ``cache`` is mutated in place (caller persists it to Drive). Returns
    ``{symbol: PriceHistory}`` keyed by normalized symbol.
    """
    results: dict[str, PriceHistory] = {}

    for symbol in normalize_all(symbols):
        entry = cache.get(symbol)
        if entry is not None and _is_fresh(entry, ttl_hours):
            results[symbol] = _price_history_from_cache(entry)
            continue
        try:
            history = _fetch_history_one(symbol, period, interval)
        except Exception as exc:  # yfinance raises a wide range of errors
            logger.warning("yfinance history fetch failed for %s: %s", symbol, exc)
            results[symbol] = (
                _price_history_from_cache(entry) if entry is not None
                else _empty_history(symbol)
            )
            continue
        cache[symbol] = dataclasses.asdict(history)
        results[symbol] = history

    return results
