import pandas as pd

from portfolio.metrics.fundamentals import held_symbols, roe, roe_history
from portfolio.models import Position

_COLS = [pd.Timestamp(year, 12, 31) for year in (2025, 2024, 2023, 2022)]


def _statement(label, values, cols=_COLS):
    """A one-row yfinance-shaped statement: index=[label], columns=period ends."""
    return pd.DataFrame({label: values}, index=cols[: len(values)]).T


def test_roe_basic():
    assert roe(100.0, 50.0) == 2.0


def test_roe_zero_equity_is_none():
    assert roe(100.0, 0.0) is None


def test_roe_none_inputs_are_none():
    assert roe(None, 50.0) is None
    assert roe(100.0, None) is None


def test_roe_nan_is_none():
    nan = float("nan")
    assert roe(nan, 50.0) is None
    assert roe(100.0, nan) is None


def test_roe_history_keyed_by_calendar_year():
    fin = _statement("Net Income", [100.0, 90.0, 80.0, 70.0])
    bs = _statement("Stockholders Equity", [50.0, 45.0, 40.0, 35.0])
    net_income, book_value, roes = roe_history(fin, bs)
    assert net_income == 100.0
    assert book_value == 50.0
    # keyed by the fiscal period-end calendar year (_COLS = 2025..2022)
    assert roes == {2025: 2.0, 2024: 2.0, 2023: 2.0, 2022: 2.0}


def test_roe_history_aligns_income_and_equity_by_year_not_position():
    # Columns deliberately out of order; income/equity must pair by YEAR.
    fcols = [pd.Timestamp(y, 12, 31) for y in (2022, 2025, 2023, 2024)]
    bcols = [pd.Timestamp(y, 12, 31) for y in (2025, 2024, 2023, 2022)]
    fin = pd.DataFrame({"Net Income": [70.0, 100.0, 80.0, 90.0]}, index=fcols).T
    bs = pd.DataFrame({"Stockholders Equity": [50.0, 45.0, 40.0, 35.0]}, index=bcols).T
    net_income, book_value, roes = roe_history(fin, bs)
    assert net_income == 100.0  # 2025 net income, even though column wasn't first
    assert book_value == 50.0   # 2025 equity
    assert roes == {2025: 2.0, 2024: 2.0, 2023: 2.0, 2022: 2.0}


def test_roe_history_missing_equity_label_yields_empty():
    fin = _statement("Net Income", [100.0, 90.0, 80.0, 70.0])
    bs = _statement("Some Other Row", [1.0, 2.0, 3.0, 4.0])
    net_income, book_value, roes = roe_history(fin, bs)
    assert net_income == 100.0
    assert book_value is None
    assert roes == {}


def test_roe_history_only_includes_years_present():
    fin = _statement("Net Income", [100.0, 90.0])
    bs = _statement("Stockholders Equity", [50.0, 45.0])
    _, _, roes = roe_history(fin, bs)
    assert roes == {2025: 2.0, 2024: 2.0}


def test_roe_history_alternate_labels():
    # Older yfinance row names still resolve.
    fin = _statement("Net Income Common Stockholders", [100.0])
    bs = _statement("Total Stockholder Equity", [50.0])
    net_income, book_value, roes = roe_history(fin, bs)
    assert (net_income, book_value) == (100.0, 50.0)
    assert roes == {2025: 2.0}


def test_roe_history_empty_statements():
    net_income, book_value, roes = roe_history(None, None)
    assert net_income is None and book_value is None
    assert roes == {}


def test_held_symbols_normalizes_dedupes_and_skips_cash():
    positions = [
        Position("11A-00003", "CMA-Edge", "BRKB", 5.0),
        Position("22B-00001", "Roth IRA-Edge", "BRK-B", 3.0),
        Position("11A-00003", "CMA-Edge", "AAPL", 10.0),
        Position("22B-00001", "Roth IRA-Edge", "IIAXX", 100.0),
    ]
    assert held_symbols(positions) == ["BRK-B", "AAPL"]
