from pathlib import Path

from portfolio.parsers.activity_parser import parse_activity_csv
from portfolio.parsers.utils import clean_symbol, parse_amount, parse_holding_base

SAMPLE = Path(__file__).parent / "sample_data" / "activity_sample.csv"


def test_only_settled_rows_kept():
    txns = parse_activity_csv(SAMPLE)
    assert len(txns) == 16
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


def test_split_legs_classified_as_split():
    # VGT 8-for-1: a +N Stock Dividend Due Bill, a -N reversal, and a +N
    # SecurityTransactions/Dividend delivery — all $0/share → SPLIT.
    txns = parse_activity_csv(SAMPLE)
    splits = [t for t in txns if t.symbol == "VGT"]
    assert len(splits) == 3
    assert all(t.tx_type == "SPLIT" for t in splits)
    assert sorted(t.quantity for t in splits) == [-70.0, 70.0, 70.0]
    assert all(t.amount == 0.0 for t in splits)


def test_cash_dividend_not_misclassified_as_split():
    # A real cash dividend (nonzero amount, no share quantity) stays DIVIDEND
    # even though "Dividend" is a split-eligible Description 1.
    txns = parse_activity_csv(SAMPLE)
    div = next(t for t in txns if t.symbol == "MCO")
    assert div.tx_type == "DIVIDEND"


def test_security_dividend_with_amount_is_not_split(tmp_path):
    # SecurityTransactions/Dividend carrying a real amount (not $0) must NOT be a
    # SPLIT — the disambiguation is amount == 0 AND a present share quantity.
    header = ('"Trade Date","Settlement Date","Pending/Settled","Account Nickname",'
              '"Account Registration","Account #","Type","Description 1 ",'
              '"Description 2","Symbol/CUSIP #","Quantity","Price ($)","Amount ($)"')
    row = ('"5/1/2026","5/1/2026","Settled","--","CMA-Edge","53X-69S37",'
           '"SecurityTransactions","Dividend","SOME FUND","ABC","--","--","12.34"')
    p = tmp_path / "activity_x.csv"
    p.write_text(header + "\n" + row + "\n", encoding="utf-8")
    tx = parse_activity_csv(p)[0]
    assert tx.tx_type != "SPLIT"


def test_funds_received_is_cash_in():
    # Phase 20: an external wire → CASH_IN with no symbol (credits cash).
    txns = parse_activity_csv(SAMPLE)
    wire = next(t for t in txns if t.tx_type == "CASH_IN" and t.amount == -30000.0)
    assert wire.symbol is None
    assert wire.account_number == "11A-00003"


def test_current_year_contribution_is_recorded_not_cash():
    # Phase 20: recorded for the paper trail, but a distinct type so cash math
    # can exclude it (the money already shows as an IIAXX deposit).
    txns = parse_activity_csv(SAMPLE)
    contrib = next(t for t in txns if t.tx_type == "CONTRIBUTION_INFO")
    assert contrib.amount == -8000.0
    assert contrib.symbol is None
    assert contrib.account_number == "22B-00001"


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


def test_parse_holding_base_extracts_share_count():
    assert parse_holding_base("VGT ETF HOLDING 10.0000 PAY DATE 05/02/2026") == 10.0
    assert parse_holding_base("HOLDING 1,234.5678 PAY DATE") == 1234.5678


def test_parse_holding_base_absent_is_none():
    assert parse_holding_base("CISCO SYSTEMS INC COM") is None
