from datetime import datetime, timedelta

import pandas as pd
import pytest

from portfolio.market import yfinance_client
from portfolio.market.yfinance_client import fetch_fundamentals, fetch_price_history

_COLS = [pd.Timestamp(year, 12, 31) for year in (2025, 2024, 2023, 2022)]
_DEFAULT_INFO = {"trailingPE": 28.4, "dividendYield": 0.0055, "returnOnEquity": 1.47}


class FakeTicker:
    def __init__(self, info, financials, balance_sheet, history=None):
        self.info = info
        self.financials = financials
        self.balance_sheet = balance_sheet
        self._history = history

    def history(self, period=None, interval=None, auto_adjust=False):
        return pd.DataFrame() if self._history is None else self._history


def _statement(label, values):
    return pd.DataFrame({label: values}, index=_COLS).T


def _fake_factory(info=None, fin=None, bs=None):
    info = _DEFAULT_INFO if info is None else info
    fin = _statement("Net Income", [94.0, 90.0, 80.0, 70.0]) if fin is None else fin
    bs = _statement("Stockholders Equity", [64.0, 60.0, 55.0, 50.0]) if bs is None else bs

    def factory(symbol):
        return FakeTicker(info, fin, bs)

    return factory


def _full_entry(symbol="AAPL", **overrides):
    entry = {
        "symbol": symbol, "pe_ratio": None, "dividend_yield": None,
        "roe_current": None, "roe_1y": None, "roe_2y": None, "roe_3y": None,
        "roe_4y": None, "net_income": None, "book_value": None,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }
    entry.update(overrides)
    return entry


def test_cache_miss_fetches_and_populates(monkeypatch):
    monkeypatch.setattr(yfinance_client, "_ticker", _fake_factory())
    cache = {}
    out = fetch_fundamentals(["AAPL"], cache, ttl_hours=24)

    f = out["AAPL"]
    assert f.pe_ratio == 28.4
    assert f.dividend_yield == 0.0055
    assert f.roe_current == 1.47
    assert f.roe_1y == pytest.approx(94.0 / 64.0)
    assert f.net_income == 94.0 and f.book_value == 64.0
    # cache was populated in place with a JSON-serializable dict
    assert cache["AAPL"]["pe_ratio"] == 28.4


def test_fresh_cache_skips_network(monkeypatch):
    def boom(symbol):
        raise AssertionError("network must not be called on a fresh cache hit")

    monkeypatch.setattr(yfinance_client, "_ticker", boom)
    cache = {"AAPL": _full_entry(pe_ratio=11.1)}
    out = fetch_fundamentals(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].pe_ratio == 11.1


def test_stale_cache_is_refetched(monkeypatch):
    monkeypatch.setattr(yfinance_client, "_ticker", _fake_factory())
    old = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    cache = {"AAPL": _full_entry(pe_ratio=1.0, fetched_at=old)}
    out = fetch_fundamentals(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].pe_ratio == 28.4  # refetched, not the stale 1.0
    assert cache["AAPL"]["pe_ratio"] == 28.4


def test_fetch_failure_keeps_stale_cache(monkeypatch):
    def boom(symbol):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(yfinance_client, "_ticker", boom)
    old = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    cache = {"AAPL": _full_entry(pe_ratio=7.0, fetched_at=old)}
    out = fetch_fundamentals(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].pe_ratio == 7.0       # stale data returned
    assert cache["AAPL"]["fetched_at"] == old  # cache left untouched


def test_fetch_failure_without_cache_returns_empty(monkeypatch):
    def boom(symbol):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(yfinance_client, "_ticker", boom)
    cache = {}
    out = fetch_fundamentals(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].symbol == "AAPL"
    assert out["AAPL"].pe_ratio is None
    assert "AAPL" not in cache  # not cached -> next run retries


def test_symbols_are_normalized_and_deduped(monkeypatch):
    monkeypatch.setattr(yfinance_client, "_ticker", _fake_factory())
    cache = {}
    out = fetch_fundamentals(["BRKB", "BRK-B"], cache, ttl_hours=24)
    assert list(out.keys()) == ["BRK-B"]


# ---------------------------------------------------------------------------
# fetch_price_history (5-yr weekly closes)
# ---------------------------------------------------------------------------

def _history_factory(close_by_date):
    """factory(symbol) -> FakeTicker whose .history() yields a Close DataFrame."""
    df = pd.DataFrame(
        {"Close": list(close_by_date.values())}, index=list(close_by_date.keys())
    )

    def factory(symbol):
        return FakeTicker(_DEFAULT_INFO, None, None, history=df)

    return factory


def _ph_entry(symbol="AAPL", dates=None, closes=None, fetched_at=None):
    return {
        "symbol": symbol,
        "dates": dates if dates is not None else ["2026-01-05"],
        "closes": closes if closes is not None else [100.0],
        "fetched_at": fetched_at or datetime.now().isoformat(timespec="seconds"),
    }


def test_price_history_cache_miss_fetches_and_populates(monkeypatch):
    closes = {pd.Timestamp("2026-01-05"): 100.0, pd.Timestamp("2026-01-12"): 105.0}
    monkeypatch.setattr(yfinance_client, "_ticker", _history_factory(closes))
    cache = {}
    out = fetch_price_history(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].dates == ["2026-01-05", "2026-01-12"]
    assert out["AAPL"].closes == [100.0, 105.0]
    assert cache["AAPL"]["closes"] == [100.0, 105.0]


def test_price_history_skips_nan_closes(monkeypatch):
    closes = {pd.Timestamp("2026-01-05"): 100.0, pd.Timestamp("2026-01-12"): float("nan")}
    monkeypatch.setattr(yfinance_client, "_ticker", _history_factory(closes))
    out = fetch_price_history(["AAPL"], {}, ttl_hours=24)
    assert out["AAPL"].dates == ["2026-01-05"]
    assert out["AAPL"].closes == [100.0]


def test_price_history_fresh_cache_skips_network(monkeypatch):
    def boom(symbol):
        raise AssertionError("network must not be called on a fresh cache hit")

    monkeypatch.setattr(yfinance_client, "_ticker", boom)
    cache = {"AAPL": _ph_entry(closes=[42.0])}
    out = fetch_price_history(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].closes == [42.0]


def test_price_history_stale_is_refetched(monkeypatch):
    closes = {pd.Timestamp("2026-02-02"): 200.0}
    monkeypatch.setattr(yfinance_client, "_ticker", _history_factory(closes))
    old = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    cache = {"AAPL": _ph_entry(closes=[1.0], fetched_at=old)}
    out = fetch_price_history(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].closes == [200.0]      # refetched, not the stale 1.0
    assert cache["AAPL"]["closes"] == [200.0]


def test_price_history_failure_keeps_stale_cache(monkeypatch):
    def boom(symbol):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(yfinance_client, "_ticker", boom)
    old = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
    cache = {"AAPL": _ph_entry(closes=[7.0], fetched_at=old)}
    out = fetch_price_history(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].closes == [7.0]            # stale data returned
    assert cache["AAPL"]["fetched_at"] == old      # cache left untouched


def test_price_history_failure_without_cache_returns_empty(monkeypatch):
    def boom(symbol):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(yfinance_client, "_ticker", boom)
    cache = {}
    out = fetch_price_history(["AAPL"], cache, ttl_hours=24)
    assert out["AAPL"].symbol == "AAPL"
    assert out["AAPL"].dates == [] and out["AAPL"].closes == []
    assert "AAPL" not in cache  # not cached -> next run retries


def test_price_history_symbols_normalized_and_deduped(monkeypatch):
    closes = {pd.Timestamp("2026-01-05"): 100.0}
    monkeypatch.setattr(yfinance_client, "_ticker", _history_factory(closes))
    out = fetch_price_history(["BRKB", "BRK-B"], {}, ttl_hours=24)
    assert list(out.keys()) == ["BRK-B"]
