"""Unit tests for portfolio/sheets/writer.py.

These tests cover the pure-Python logic (normalization, dedup key construction,
and mocked Sheets writes) without requiring a live gspread connection.
"""

from datetime import date
from unittest.mock import MagicMock

import pytest

from portfolio.models import CashBalance, Position, RunLogEntry, Transaction
from portfolio.sheets.writer import (
    _norm,
    _tx_canonical_key,
    _row_canonical_key,
    _read_tab_rows,
    load_existing_transaction_keys,
    load_existing_source_files,
    write_transactions,
    write_holdings,
    write_cash,
    write_stock_metrics,
    write_run_log,
    TAB_TRANSACTIONS,
    TAB_HOLDINGS,
    TAB_CASH,
    TAB_STOCK_METRICS,
    TAB_RUN_LOG,
    TRANSACTIONS_HEADERS,
    HOLDINGS_HEADERS,
    CASH_HEADERS,
    STOCK_METRICS_HEADERS,
    RUN_LOG_HEADERS,
)


# ---------------------------------------------------------------------------
# _norm
# ---------------------------------------------------------------------------

def test_norm_none():
    assert _norm(None) == ""

def test_norm_empty_string():
    assert _norm("") == ""

def test_norm_dash_dash():
    assert _norm("--") == ""

def test_norm_date():
    assert _norm(date(2026, 1, 30)) == "2026-01-30"

def test_norm_float():
    assert _norm(30.576) == "30.57600000"

def test_norm_negative_float():
    assert _norm(-3211.38) == "-3211.38000000"

def test_norm_int():
    assert _norm(5) == "5.00000000"

def test_norm_string_that_parses_as_float():
    assert _norm("30.576") == "30.57600000"

def test_norm_string_date_iso():
    # ISO date strings are NOT parseable as float → returned verbatim
    assert _norm("2026-01-30") == "2026-01-30"

def test_norm_account_string():
    assert _norm("11A-00003") == "11A-00003"

def test_norm_bool_not_treated_as_int():
    # bools fall through to str branch; not formatted as ints
    assert _norm(True) == "True"


# ---------------------------------------------------------------------------
# _tx_canonical_key round-trip consistency
# ---------------------------------------------------------------------------

def _make_tx(tx_type="BUY", symbol="AAPL", quantity=10.0, price=150.0,
             amount=-1500.0, account="11A-00003",
             trade_date=date(2026, 1, 15), settlement_date=date(2026, 1, 17),
             source_file="activity.csv"):
    return Transaction(
        trade_date=trade_date,
        settlement_date=settlement_date,
        status="Settled",
        account_number=account,
        account_registration="CMA-Edge",
        tx_type=tx_type,
        description="Apple",
        symbol=symbol,
        quantity=quantity,
        price=price,
        amount=amount,
        source_file=source_file,
    )


def test_tx_canonical_key_buy():
    tx = _make_tx()
    key = _tx_canonical_key(tx)
    assert key == (
        "2026-01-15",  # trade_date
        "2026-01-17",  # settlement_date
        "11A-00003",
        "BUY",
        "AAPL",
        "10.00000000",
        "-1500.00000000",
    )


def test_tx_canonical_key_init_buy():
    tx = _make_tx(tx_type="INIT_BUY", source_file="INIT:Unrealized_022026.csv")
    key = _tx_canonical_key(tx)
    assert key[0] == "INIT"
    assert key[1] == "11A-00003"
    assert key[2] == "AAPL"
    assert key[3] == "2026-01-15"
    assert key[4] == "10.00000000"
    assert key[5] == "150.00000000"


def test_tx_canonical_key_none_fields():
    tx = _make_tx(tx_type="DIVIDEND", symbol="AAPL", quantity=None, price=None,
                  amount=25.50)
    # symbol is not None here but quantity/price are
    key = _tx_canonical_key(tx)
    assert key[5] == ""   # quantity None → ""
    assert key[6] == "25.50000000"


def test_tx_canonical_key_none_symbol():
    tx = _make_tx(tx_type="CASH_IN", symbol=None, quantity=None, price=None,
                  amount=1000.0)
    key = _tx_canonical_key(tx)
    assert key[4] == ""   # symbol None → ""


# ---------------------------------------------------------------------------
# _row_canonical_key
# ---------------------------------------------------------------------------

def test_row_canonical_key_buy():
    row = {
        "trade_date": "2026-01-15",
        "settlement_date": "2026-01-17",
        "account_number": "11A-00003",
        "tx_type": "BUY",
        "symbol": "AAPL",
        "quantity": "10.0",
        "price": "150.0",
        "amount": "-1500.0",
    }
    key = _row_canonical_key(row)
    assert key == (
        "2026-01-15",
        "2026-01-17",
        "11A-00003",
        "BUY",
        "AAPL",
        "10.00000000",
        "-1500.00000000",
    )


def test_row_canonical_key_init_buy():
    row = {
        "trade_date": "2026-01-15",
        "settlement_date": "2026-01-15",
        "account_number": "11A-00003",
        "tx_type": "INIT_BUY",
        "symbol": "AXP",
        "quantity": "5.0",
        "price": "164.35",
        "amount": "-821.75",
        "source_file": "INIT:Unrealized_022026.csv",
    }
    key = _row_canonical_key(row)
    assert key[0] == "INIT"
    assert key[1] == "11A-00003"
    assert key[2] == "AXP"
    assert key[3] == "2026-01-15"
    assert key[4] == "5.00000000"
    assert key[5] == "164.35000000"


def test_row_canonical_key_empty_symbol():
    row = {
        "trade_date": "2026-01-15",
        "settlement_date": "2026-01-17",
        "account_number": "11A-00003",
        "tx_type": "CASH_IN",
        "symbol": "",
        "quantity": "",
        "amount": "1000.0",
    }
    key = _row_canonical_key(row)
    assert key[4] == ""
    assert key[5] == ""


# ---------------------------------------------------------------------------
# _tx_canonical_key / _row_canonical_key round-trip match
# ---------------------------------------------------------------------------

def test_dedup_key_round_trip_buy():
    """A key written then read back via row dict must match the original."""
    tx = _make_tx()
    tx_key = _tx_canonical_key(tx)

    # Simulate round-trip: write the tx fields to a row dict then reconstruct
    row = {
        "trade_date": tx.trade_date.isoformat(),
        "settlement_date": tx.settlement_date.isoformat(),
        "account_number": tx.account_number,
        "tx_type": tx.tx_type,
        "symbol": tx.symbol or "",
        "quantity": str(tx.quantity) if tx.quantity is not None else "",
        "price": str(tx.price) if tx.price is not None else "",
        "amount": str(tx.amount),
    }
    assert _row_canonical_key(row) == tx_key


def test_dedup_key_round_trip_init_buy():
    tx = _make_tx(tx_type="INIT_BUY")
    tx_key = _tx_canonical_key(tx)
    row = {
        "trade_date": tx.trade_date.isoformat(),
        "settlement_date": tx.settlement_date.isoformat(),
        "account_number": tx.account_number,
        "tx_type": tx.tx_type,
        "symbol": tx.symbol or "",
        "quantity": str(tx.quantity) if tx.quantity is not None else "",
        "price": str(tx.price) if tx.price is not None else "",
        "amount": str(tx.amount),
    }
    assert _row_canonical_key(row) == tx_key


# ---------------------------------------------------------------------------
# _read_tab_rows
# ---------------------------------------------------------------------------

def test_read_tab_rows_basic():
    ws = MagicMock()
    ws.get_all_values.return_value = [
        ["trade_date", "symbol", "amount"],
        ["2026-01-15", "AAPL", "-1500.0"],
        ["2026-01-16", "GOOG", "-2000.0"],
    ]
    rows = _read_tab_rows(ws)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAPL"
    assert rows[1]["amount"] == "-2000.0"


def test_read_tab_rows_skips_blank():
    ws = MagicMock()
    ws.get_all_values.return_value = [
        ["trade_date", "symbol"],
        ["2026-01-15", "AAPL"],
        ["", ""],
        ["2026-01-16", "GOOG"],
    ]
    rows = _read_tab_rows(ws)
    assert len(rows) == 2


def test_read_tab_rows_empty_sheet():
    ws = MagicMock()
    ws.get_all_values.return_value = [["trade_date", "symbol"]]
    assert _read_tab_rows(ws) == []


def test_read_tab_rows_pads_short_rows():
    ws = MagicMock()
    ws.get_all_values.return_value = [
        ["col_a", "col_b", "col_c"],
        ["val_a", "val_b"],  # missing col_c
    ]
    rows = _read_tab_rows(ws)
    assert rows[0]["col_c"] == ""


# ---------------------------------------------------------------------------
# load_existing_transaction_keys
# ---------------------------------------------------------------------------

def _tab_mock(title: str, rows: list[list[str]]) -> MagicMock:
    """Mock Worksheet with a given title and get_all_values return."""
    ws = MagicMock()
    ws.title = title
    ws.get_all_values.return_value = rows
    return ws


def _sh_with_tabs(*tab_mocks) -> MagicMock:
    """Mock Spreadsheet whose worksheets() returns the given tab mocks."""
    sh = MagicMock()
    sh.worksheets.return_value = list(tab_mocks)
    # worksheet(name) returns the matching tab mock
    def _get_ws(name):
        for t in tab_mocks:
            if t.title == name:
                return t
        raise RuntimeError(f"tab {name!r} not found in mock")
    sh.worksheet.side_effect = _get_ws
    return sh


def test_load_existing_keys_missing_tab():
    sh = MagicMock()
    sh.worksheets.return_value = []   # no tabs at all
    keys = load_existing_transaction_keys(sh)
    assert keys == set()


def test_load_existing_keys_populated():
    ws = _tab_mock(TAB_TRANSACTIONS, [
        TRANSACTIONS_HEADERS,
        ["2026-01-15", "2026-01-17", "Settled", "11A-00003", "CMA-Edge",
         "BUY", "Apple", "AAPL", "10.0", "150.0", "-1500.0", "act.csv"],
    ])
    sh = _sh_with_tabs(ws)
    keys = load_existing_transaction_keys(sh)
    assert len(keys) == 1
    expected = _tx_canonical_key(_make_tx(source_file="act.csv"))
    assert expected in keys


def test_load_existing_source_files():
    ws = _tab_mock(TAB_TRANSACTIONS, [
        TRANSACTIONS_HEADERS,
        ["2026-01-15", "2026-01-15", "Settled", "11A-00003", "CMA-Edge",
         "INIT_BUY", "AXP", "AXP", "5.0", "164.35", "-821.75",
         "INIT:Unrealized_022026.csv"],
        ["2026-01-16", "2026-01-17", "Settled", "11A-00003", "CMA-Edge",
         "BUY", "AAPL", "AAPL", "10.0", "150.0", "-1500.0", "activity.csv"],
    ])
    sh = _sh_with_tabs(ws)
    files = load_existing_source_files(sh)
    assert "INIT:Unrealized_022026.csv" in files
    assert "activity.csv" in files


# ---------------------------------------------------------------------------
# write_transactions
# ---------------------------------------------------------------------------

def _mock_sh_with_tab(tab_name=TAB_TRANSACTIONS):
    """Return (sh, ws) mock pair with a pre-existing tab."""
    ws = MagicMock()
    ws.title = tab_name
    sh = MagicMock()
    sh.worksheets.return_value = [ws]
    sh.worksheet.return_value = ws
    return sh, ws


def test_write_transactions_appends_new():
    sh, ws = _mock_sh_with_tab()
    tx = _make_tx()
    added = write_transactions(sh, [tx], existing_keys=set())
    assert added == 1
    ws.append_rows.assert_called_once()
    rows_written = ws.append_rows.call_args[0][0]
    assert len(rows_written) == 1
    assert rows_written[0][7] == "AAPL"  # symbol column


def test_write_transactions_skips_existing():
    sh, ws = _mock_sh_with_tab()
    tx = _make_tx()
    key = _tx_canonical_key(tx)
    added = write_transactions(sh, [tx], existing_keys={key})
    assert added == 0
    ws.append_rows.assert_not_called()


def test_write_transactions_none_symbol_written_as_empty():
    sh, ws = _mock_sh_with_tab()
    tx = _make_tx(tx_type="CASH_IN", symbol=None, quantity=None, price=None,
                  amount=500.0)
    write_transactions(sh, [tx], existing_keys=set())
    rows_written = ws.append_rows.call_args[0][0]
    assert rows_written[0][7] == ""   # symbol
    assert rows_written[0][8] == ""   # quantity
    assert rows_written[0][9] == ""   # price


def test_write_transactions_creates_tab_if_missing():
    ws = MagicMock()
    ws.title = TAB_TRANSACTIONS
    sh = MagicMock()
    sh.worksheets.return_value = []   # tab not found initially
    sh.add_worksheet.return_value = ws
    tx = _make_tx()
    write_transactions(sh, [tx], existing_keys=set())
    sh.add_worksheet.assert_called_once()


def test_write_transactions_no_duplicates_from_same_batch():
    """write_transactions does not inter-dedup within the batch — that's the runner's job."""
    sh, ws = _mock_sh_with_tab()
    tx1 = _make_tx(tx_type="INIT_BUY", quantity=5.0, price=164.35)
    tx2 = _make_tx(tx_type="INIT_BUY", quantity=5.0, price=164.35)  # identical AXP-style lot
    # Neither key is in existing_keys so both should be written
    added = write_transactions(sh, [tx1, tx2], existing_keys=set())
    assert added == 2


# ---------------------------------------------------------------------------
# write_holdings
# ---------------------------------------------------------------------------

def _make_position(symbol="AAPL", quantity=10.0, account="11A-00003"):
    from portfolio.models import Lot
    lot = Lot(
        account_number=account,
        symbol=symbol,
        acquisition_date=date(2026, 1, 15),
        quantity=quantity,
        unit_cost=150.0,
    )
    return Position(
        account_number=account,
        account_registration="CMA-Edge",
        symbol=symbol,
        quantity=quantity,
        lots=[lot],
    )


def _holdings_sh():
    sh, ws = _mock_sh_with_tab(TAB_HOLDINGS)
    return sh, ws


def test_write_holdings_clears_and_rewrites():
    sh, ws = _holdings_sh()
    pos = _make_position()
    write_holdings(sh, [pos])
    ws.clear.assert_called_once()
    ws.append_row.assert_called_once_with(HOLDINGS_HEADERS, value_input_option="USER_ENTERED")
    ws.append_rows.assert_called_once()


def test_write_holdings_googfinance_formulas():
    sh, ws = _holdings_sh()
    pos = _make_position()
    write_holdings(sh, [pos])
    rows = ws.append_rows.call_args[0][0]
    assert len(rows) == 1
    row = rows[0]
    # current_price formula references symbol in column D, row 2
    assert 'GOOGFINANCE(D2,"price")' in row[7]
    # market_value formula references quantity (E) and price (H)
    assert "E2*H2" in row[8]


def test_write_holdings_empty_positions():
    sh, ws = _holdings_sh()
    write_holdings(sh, [])
    ws.append_rows.assert_not_called()


def test_write_holdings_sorted_by_account_then_symbol():
    sh, ws = _holdings_sh()
    positions = [
        _make_position("TSLA", account="11A-00002"),
        _make_position("AAPL", account="11A-00003"),
        _make_position("GOOG", account="11A-00002"),
    ]
    write_holdings(sh, positions)
    rows = ws.append_rows.call_args[0][0]
    symbols = [r[3] for r in rows]
    assert symbols == ["GOOG", "TSLA", "AAPL"]





# ---------------------------------------------------------------------------
# write_cash
# ---------------------------------------------------------------------------

def _make_cash_balance(account="11A-00003", reconstructed=1000.0, snapshot=990.0):
    return CashBalance(
        account_number=account,
        account_registration="CMA-Edge",
        cash_account="990156937",
        reconstructed=reconstructed,
        snapshot=snapshot,
        as_of_date=date(2026, 1, 30),
    )


def _cash_sh():
    return _mock_sh_with_tab(TAB_CASH)


def test_write_cash_clears_and_rewrites():
    sh, ws = _cash_sh()
    bal = _make_cash_balance()
    write_cash(sh, [bal])
    ws.clear.assert_called_once()
    ws.append_rows.assert_called_once()


def test_write_cash_drift_in_row():
    sh, ws = _cash_sh()
    bal = _make_cash_balance(reconstructed=1000.0, snapshot=990.0)
    write_cash(sh, [bal])
    rows = ws.append_rows.call_args[0][0]
    row = rows[0]
    assert row[5] == 1000.0   # reconstructed
    assert row[6] == 990.0    # snapshot
    assert row[7] == 10.0     # drift


def test_write_cash_no_snapshot():
    sh, ws = _cash_sh()
    bal = CashBalance(
        account_number="11A-00003",
        account_registration="CMA-Edge",
        cash_account="990156937",
        reconstructed=1000.0,
        snapshot=None,
        as_of_date=date(2026, 1, 30),
    )
    write_cash(sh, [bal])
    rows = ws.append_rows.call_args[0][0]
    assert rows[0][6] == ""   # snapshot empty
    assert rows[0][7] == ""   # drift empty


# ---------------------------------------------------------------------------
# write_stock_metrics
# ---------------------------------------------------------------------------

def _make_fundamentals():
    from portfolio.market.yfinance_client import StockFundamentals
    return {
        "AAPL": StockFundamentals(
            symbol="AAPL", pe_ratio=28.4, dividend_yield=0.0055,
            roe_current=1.47, roe_1y=1.60, roe_2y=1.55, roe_3y=1.45, roe_4y=1.30,
            net_income=94_000_000_000, book_value=64_000_000_000,
            fetched_at="2026-01-30T10:00:00",
        ),
    }


def _metrics_sh():
    return _mock_sh_with_tab(TAB_STOCK_METRICS)


def test_write_stock_metrics_clears_and_rewrites():
    sh, ws = _metrics_sh()
    write_stock_metrics(sh, _make_fundamentals(), date(2026, 1, 30))
    ws.clear.assert_called_once()
    ws.append_rows.assert_called_once()


def test_write_stock_metrics_googfinance_formulas():
    sh, ws = _metrics_sh()
    write_stock_metrics(sh, _make_fundamentals(), date(2026, 1, 30))
    rows = ws.append_rows.call_args[0][0]
    row = rows[0]
    # AAPL is in row 2, symbol in column B
    assert 'GOOGFINANCE(B2,"price")' in row[11]   # current_price
    assert 'GOOGFINANCE(B2,"high52")' in row[12]  # high_52wk
    assert 'GOOGFINANCE(B2,"low52")' in row[13]   # low_52wk


def test_write_stock_metrics_none_fields_written_as_empty():
    from portfolio.market.yfinance_client import StockFundamentals
    sh, ws = _metrics_sh()
    fundamentals = {
        "IX": StockFundamentals(
            symbol="IX", pe_ratio=None, dividend_yield=None,
            roe_current=None, roe_1y=None, roe_2y=None, roe_3y=None, roe_4y=None,
            net_income=None, book_value=None, fetched_at="2026-01-30T10:00:00",
        )
    }
    write_stock_metrics(sh, fundamentals, date(2026, 1, 30))
    rows = ws.append_rows.call_args[0][0]
    row = rows[0]
    # pe_ratio through book_value (indices 2-10) should all be ""
    for i in range(2, 11):
        assert row[i] == "", f"index {i} should be empty"


# ---------------------------------------------------------------------------
# write_run_log
# ---------------------------------------------------------------------------

def test_write_run_log_appends():
    sh, ws = _mock_sh_with_tab(TAB_RUN_LOG)
    entry = RunLogEntry(
        run_timestamp="2026-01-30T10:00:00",
        files_processed=2,
        init_rows_added=45,
        transactions_added=12,
        accounts_skipped="",
        errors="",
        holdings_changed="AAPL: 10→15",
        cash_reconciliation="11A-00003: drift=0.00",
        duration_sec=4.2,
        notes="bootstrap run",
    )
    write_run_log(sh, entry)
    ws.append_row.assert_called_once()
    row = ws.append_row.call_args[0][0]
    assert row[0] == "2026-01-30T10:00:00"
    assert row[1] == 2
    assert row[8] == 4.2
