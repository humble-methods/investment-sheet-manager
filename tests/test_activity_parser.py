from pathlib import Path

from portfolio.parsers.activity_parser import parse_activity_csv
from portfolio.parsers.utils import clean_symbol, parse_amount

SAMPLE = Path(__file__).parent / "sample_data" / "activity_sample.csv"


def test_only_settled_rows_kept():
    txns = parse_activity_csv(SAMPLE)
    assert len(txns) == 11
    assert all(t.status == "Settled" for t in txns)


def test_pending_row_skipped():
    txns = parse_activity_csv(SAMPLE)
    assert not any(t.symbol == "GOOG" for t in txns)


def test_buy_purchase():
    txns = parse_activity_csv(SAMPLE)
    buy = next(t for t in txns if t.symbol == "CSCO")
    assert buy.tx_type == "BUY"
    assert buy.quantity == 25.0
    assert buy.price == 128.46
    assert buy.amount == -3211.38


def test_sell():
    txns = parse_activity_csv(SAMPLE)
    sell = next(t for t in txns if t.symbol == "CVSA")
    assert sell.tx_type == "SELL"
    assert sell.quantity == -84.0
    assert sell.amount == 10425.03


def test_dividend_quantity_and_price_are_none():
    txns = parse_activity_csv(SAMPLE)
    div = next(t for t in txns if t.symbol == "MCO")
    assert div.tx_type == "DIVIDEND"
    assert div.quantity is None
    assert div.price is None
    assert div.amount == 41.20


def test_cash_deposit_has_no_symbol():
    txns = parse_activity_csv(SAMPLE)
    deposits = [t for t in txns if t.tx_type == "CASH_IN"]
    assert deposits
    assert all(d.symbol is None for d in deposits)
    assert any(d.amount == -19.0 for d in deposits)
    assert any(d.amount == -41412.0 for d in deposits)


def test_description_boilerplate_stripped():
    txns = parse_activity_csv(SAMPLE)

    buy = next(t for t in txns if t.symbol == "CSCO")
    assert buy.description == "CISCO SYSTEMS INC COM"

    # COVISTA row leads with the "ACTUAL PRICES, REMUNERATION..." variant
    sell = next(t for t in txns if t.symbol == "CVSA")
    assert sell.description == "COVISTA INC"


def test_adr_fee_and_tax_withholding_are_negative():
    txns = parse_activity_csv(SAMPLE)

    fee = next(t for t in txns if t.tx_type == "ADR_FEE")
    assert fee.amount == -1.68

    tax = next(t for t in txns if t.tx_type == "TAX_WITHHOLDING")
    assert tax.amount == -2.34


# --- utils ---

def test_parse_amount_parens_is_negative():
    assert parse_amount("(3,211.38)") == -3211.38


def test_parse_amount_dashes_and_empty_are_none():
    assert parse_amount("--") is None
    assert parse_amount("") is None


def test_parse_amount_integer_like_is_float():
    assert parse_amount("19") == 19.0


def test_clean_symbol_applies_override():
    assert clean_symbol("BRKB") == "BRK-B"


def test_clean_symbol_cash_cusip_is_none():
    assert clean_symbol("--", cusip="990156937") is None
