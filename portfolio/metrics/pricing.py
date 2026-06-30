"""Shared price-series helpers: close-on-or-before lookup + anniversary dates.

Kept dependency-light (stdlib only) so both the Performance metrics and the Sheets
writer can use them without pulling in yfinance/pandas — yfinance is lazily imported
elsewhere specifically so the offline test suite stays fast.
"""

from __future__ import annotations

from datetime import date


def close_on_or_before(history, as_of: date) -> float | None:
    """Last close on/before ``as_of`` from a PriceHistory-like object.

    ``history`` is duck-typed: any object exposing parallel ``dates`` (ISO
    "YYYY-MM-DD" strings, ascending) and ``closes`` lists. Returns None when
    ``history`` is None/empty or every date is after ``as_of``. Shared by the
    Performance year-boundary valuations and the Price History anniversary view.
    """
    if history is None:
        return None
    cutoff = as_of.isoformat()
    best = None
    for d, c in zip(history.dates, history.closes):
        if d <= cutoff:
            best = c
        else:
            break  # dates are ascending
    return best


def anniversary(today: date, years_back: int) -> date:
    """``today`` shifted back ``years_back`` calendar years.

    Feb 29 has no counterpart in non-leap years → falls back to Feb 28.
    """
    try:
        return today.replace(year=today.year - years_back)
    except ValueError:
        return today.replace(year=today.year - years_back, day=28)
