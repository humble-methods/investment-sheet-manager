"""Fundamental-metric helpers: ROE history from yfinance financial statements.

Pure computation — no network and no yfinance import. yfinance_client feeds the
raw ``financials`` / ``balance_sheet`` DataFrames in; these helpers turn them
into ROE numbers. Kept dependency-light (duck-typed over the DataFrame API) so
the math is unit-testable without hitting the network.
"""

from portfolio.market.symbol_overrides import normalize_all
from portfolio.models import Position

# yfinance has renamed these statement rows across versions; try each in order.
NET_INCOME_LABELS = (
    "Net Income",
    "Net Income Common Stockholders",
    "Net Income Continuous Operations",
    "NetIncome",
)
STOCKHOLDERS_EQUITY_LABELS = (
    "Stockholders Equity",
    "Total Stockholder Equity",
    "Common Stock Equity",
    "Total Equity Gross Minority Interest",
)

# yfinance returns ~4 annual columns per fetch. We collect every column it gives
# (capped generously) and KEY each ROE by the fiscal period-end's calendar year so
# the cache can accumulate years forward across runs (see yfinance_client) — the
# sheet is provisioned for 10 years even though any single fetch yields ~4.
MAX_ROE_COLS = 12


def _is_nan(value) -> bool:
    # NaN is the only value not equal to itself; avoids importing math/numpy.
    return value != value


def roe(net_income, equity) -> float | None:
    """net_income / equity, or None when either is missing or equity is ~0."""
    if net_income is None or equity is None:
        return None
    if _is_nan(net_income) or _is_nan(equity) or equity == 0:
        return None
    return net_income / equity


def _column_year(col) -> int | None:
    """Calendar year of a statement column label (a fiscal period-end), or None."""
    year = getattr(col, "year", None)
    if isinstance(year, int):
        return year
    try:
        return int(str(col)[:4])
    except (ValueError, TypeError):
        return None


def _annual_pairs(statement, labels, max_cols: int = MAX_ROE_COLS) -> list[tuple[int, float | None]]:
    """``[(calendar_year, value), ...]`` for the first matching label, newest first.

    ``statement`` is a yfinance financials/balance_sheet DataFrame (or None).
    ``calendar_year`` is the year of each column's period-end date; columns whose
    label can't be resolved to a year are skipped. Empty if the statement is
    missing or none of ``labels`` are present.
    """
    if statement is None or getattr(statement, "empty", True):
        return []
    index = list(statement.index)
    row = None
    for label in labels:
        if label in index:
            row = statement.loc[label]
            break
    if row is None:
        return []
    # yfinance orders columns newest-first already; sort defensively (period-end
    # dates are comparable). Fall back to the given order if columns can't sort.
    try:
        columns = sorted(statement.columns, reverse=True)
    except TypeError:
        columns = list(statement.columns)
    pairs: list[tuple[int, float | None]] = []
    for col in columns[:max_cols]:
        year = _column_year(col)
        if year is None:
            continue
        value = row[col]
        pairs.append((year, None if _is_nan(value) else float(value)))
    return pairs


def roe_history(financials, balance_sheet, max_cols: int = MAX_ROE_COLS):
    """``(latest_net_income, latest_book_value, {calendar_year: roe})``.

    ROE for year *Y* = net_income_Y / stockholders_equity_Y, keyed by the fiscal
    period-end's **calendar year** so callers can accumulate history across runs.
    Only years computable in BOTH statements (non-None, equity != 0) are included.
    The ``latest_*`` values are the most recent annual figures (newest column),
    used for the Stock Metrics ``net_income`` / ``book_value`` columns.
    """
    income_pairs = _annual_pairs(financials, NET_INCOME_LABELS, max_cols)
    equity_pairs = _annual_pairs(balance_sheet, STOCKHOLDERS_EQUITY_LABELS, max_cols)

    equity_by_year = dict(equity_pairs)
    roe_by_year: dict[int, float] = {}
    for year, net in income_pairs:
        r = roe(net, equity_by_year.get(year))
        if r is not None:
            roe_by_year[year] = r

    latest_net_income = income_pairs[0][1] if income_pairs else None
    latest_book_value = equity_pairs[0][1] if equity_pairs else None
    return latest_net_income, latest_book_value, roe_by_year


def held_symbols(positions: list[Position]) -> list[str]:
    """Normalized, de-duped yfinance symbols for the held positions.

    Drops cash/blank symbols; safe to feed straight into fetch_fundamentals.
    """
    return normalize_all(p.symbol for p in positions if p.symbol)
