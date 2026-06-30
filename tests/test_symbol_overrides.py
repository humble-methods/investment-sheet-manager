from portfolio.market.symbol_overrides import is_cash, normalize_all, normalize_symbol


def test_normalize_known_override():
    assert normalize_symbol("BRKB") == "BRK-B"


def test_normalize_is_idempotent():
    # Held-position symbols are already normalized; re-normalizing must no-op.
    assert normalize_symbol("BRK-B") == "BRK-B"


def test_normalize_applies_ticker_rename():
    # ATGE (Adtalem) renamed to CVSA (Covista) — old ticker maps to current one so
    # bootstrap lots and later activity unify. See TICKER_RENAMES in config.
    assert normalize_symbol("ATGE") == "CVSA"


def test_rename_then_spelling_override(monkeypatch):
    # A renamed ticker must still pick up a Merrill->Yahoo spelling fix afterward.
    from portfolio.market import symbol_overrides as so

    monkeypatch.setattr(so, "TICKER_RENAMES", {"OLD": "BRKB"})
    monkeypatch.setattr(so, "SYMBOL_OVERRIDES", {"BRKB": "BRK-B"})
    assert so.normalize_symbol("OLD") == "BRK-B"


def test_normalize_all_collapses_renamed_and_current_ticker():
    # A sheet holding both the stale (ATGE) and current (CVSA) ticker collapses to one.
    assert normalize_all(["ATGE", "CVSA"]) == ["CVSA"]


def test_normalize_passthrough_and_strips():
    assert normalize_symbol("  AAPL ") == "AAPL"


def test_is_cash_detects_mmkt_and_sweep():
    assert is_cash("IIAXX") is True
    assert is_cash("--", cusip="990156937") is True
    assert is_cash("990156937") is True


def test_is_cash_false_for_equity():
    assert is_cash("AAPL") is False


def test_normalize_all_dedupes_normalizes_and_drops_cash():
    out = normalize_all(["BRKB", "BRK-B", "AAPL", "AAPL", "IIAXX", "", "--", None])
    assert out == ["BRK-B", "AAPL"]
