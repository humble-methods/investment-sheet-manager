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

MAX_ROE_YEARS = 4


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


def _annual_series(statement, labels, max_cols: int = MAX_ROE_YEARS) -> list[float | None]:
    """Row values for the first matching label, newest fiscal year first.

    ``statement`` is a yfinance financials/balance_sheet DataFrame (or None).
    Returns a list of floats/None, at most ``max_cols`` long. Empty if the
    statement is missing or none of ``labels`` are present.
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
    series: list[float | None] = []
    for col in columns[:max_cols]:
        value = row[col]
        series.append(None if _is_nan(value) else float(value))
    return series


def roe_history(financials, balance_sheet, max_years: int = MAX_ROE_YEARS):
    """(latest_net_income, latest_book_value, [roe_year1 .. roe_yearN]).

    ``roe_yearI`` = net_income_I / stockholders_equity_I for the I-th most recent
    fiscal year. The list is always ``max_years`` long (None-padded). The
    ``latest_*`` values are the most recent annual figures (column 0), used as
    the Stock Metrics ``net_income`` / ``book_value`` columns.
    """
    net_income = _annual_series(financials, NET_INCOME_LABELS, max_years)
    equity = _annual_series(balance_sheet, STOCKHOLDERS_EQUITY_LABELS, max_years)

    roes: list[float | None] = []
    for i in range(max_years):
        n = net_income[i] if i < len(net_income) else None
        e = equity[i] if i < len(equity) else None
        roes.append(roe(n, e))

    latest_net_income = net_income[0] if net_income else None
    latest_book_value = equity[0] if equity else None
    return latest_net_income, latest_book_value, roes


def held_symbols(positions: list[Position]) -> list[str]:
    """Normalized, de-duped yfinance symbols for the held positions.

    Drops cash/blank symbols; safe to feed straight into fetch_fundamentals.
    """
    return normalize_all(p.symbol for p in positions if p.symbol)
