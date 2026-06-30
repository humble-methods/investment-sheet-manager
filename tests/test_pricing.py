"""Unit tests for portfolio/metrics/pricing.py (shared price-series helpers)."""

from datetime import date
from types import SimpleNamespace

from portfolio.metrics.pricing import anniversary, close_on_or_before


def _hist(dates, closes):
    return SimpleNamespace(dates=dates, closes=closes)


def test_close_on_or_before_picks_last_on_or_before():
    h = _hist(["2025-01-01", "2025-06-01", "2026-01-01"], [10.0, 20.0, 30.0])
    assert close_on_or_before(h, date(2025, 6, 15)) == 20.0   # 06-01, not the later 01-01
    assert close_on_or_before(h, date(2026, 1, 1)) == 30.0    # exact match is included


def test_close_on_or_before_before_first_returns_none():
    h = _hist(["2025-01-01"], [10.0])
    assert close_on_or_before(h, date(2024, 12, 31)) is None


def test_close_on_or_before_none_or_empty():
    assert close_on_or_before(None, date(2026, 1, 1)) is None
    assert close_on_or_before(_hist([], []), date(2026, 1, 1)) is None


def test_anniversary_basic():
    assert anniversary(date(2026, 6, 15), 0) == date(2026, 6, 15)
    assert anniversary(date(2026, 6, 15), 5) == date(2021, 6, 15)


def test_anniversary_leap_day_falls_back_to_feb_28():
    # 2024-02-29 has no counterpart in 2023 → Feb 28
    assert anniversary(date(2024, 2, 29), 1) == date(2023, 2, 28)
