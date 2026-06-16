"""Google Sheets output: Transactions, Holdings, Cash, Stock Metrics, Run Log."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from portfolio.config import ACCOUNT_OWNERS
from portfolio.models import CashBalance, Position, RunLogEntry, Transaction

if TYPE_CHECKING:
    from portfolio.market.yfinance_client import PriceHistory, StockFundamentals

logger = logging.getLogger(__name__)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

TAB_TRANSACTIONS = "Transactions"
TAB_HOLDINGS = "Holdings"
TAB_CASH = "Cash"
TAB_STOCK_METRICS = "Stock Metrics"
TAB_RUN_LOG = "Run Log"
TAB_WATCHLIST = "Watchlist"
TAB_PRICE_HISTORY = "Price History"

# Displayed header labels are Title Case (human-readable) and decoupled from the
# internal field keys used for dedup read-back. Only the Transactions tab is ever
# read back, so only it needs a parallel *_KEYS list; its column ORDER is unchanged
# (no as_of_date), which keeps already-deployed sheets readable by position even
# after the header is relabeled.
TRANSACTIONS_HEADERS = [
    "Trade Date", "Settlement Date", "Status", "Account Number",
    "Account Registration", "Transaction Type", "Description", "Symbol",
    "Quantity", "Price", "Amount", "Source File",
]
TRANSACTIONS_KEYS = [
    "trade_date", "settlement_date", "status", "account_number",
    "account_registration", "tx_type", "description", "symbol",
    "quantity", "price", "amount", "source_file",
]
# A=Account Number B=Account Registration C=Symbol D=Quantity E=Average Cost
# F=Cost Basis G=Current Price H=Market Value I=As Of Date
HOLDINGS_HEADERS = [
    "Account Number", "Account Registration", "Symbol", "Quantity",
    "Average Cost", "Cost Basis", "Current Price", "Market Value", "As Of Date",
]
CASH_HEADERS = [
    "Account Number", "Account Registration", "Owner", "Cash Account",
    "Reconstructed", "Snapshot", "Drift", "As Of Date",
]
# A=Symbol B=P/E Ratio C=Dividend Yield D=ROE (Current) E=ROE (1Y Ago)
# F=ROE (2Y Ago) G=ROE (3Y Ago) H=ROE (4Y Ago) I=Net Income J=Book Value
# K=Current Price L=As Of Date
# 5-year weekly closes live on the separate Price History tab (fetched by Python).
STOCK_METRICS_HEADERS = [
    "Symbol", "P/E Ratio", "Dividend Yield", "ROE (Current)",
    "ROE (1Y Ago)", "ROE (2Y Ago)", "ROE (3Y Ago)", "ROE (4Y Ago)",
    "Net Income", "Book Value", "Current Price", "As Of Date",
]
RUN_LOG_HEADERS = [
    "Run Timestamp", "Files Processed", "Init Rows Added", "Transactions Added",
    "Accounts Skipped", "Errors", "Holdings Changed", "Cash Reconciliation",
    "Duration (Sec)", "Notes",
]
# Human-maintained intake tab: one ticker per row under "Symbol".
WATCHLIST_HEADERS = ["Symbol"]


def get_gspread_client(credentials=None):
    """Return an authenticated gspread client.

    Pass ``credentials`` (Colab: from ``google.auth.default()``) for OAuth2.
    Without credentials, performs local InstalledAppFlow from ``credentials.json``.
    """
    import gspread

    if credentials is not None:
        return gspread.authorize(credentials)

    import pickle
    from pathlib import Path

    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path("token.pickle")
    creds = None

    if token_path.exists():
        with token_path.open("rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                "credentials.json", SCOPES
            )
            creds = flow.run_local_server(port=0)
        with token_path.open("wb") as f:
            pickle.dump(creds, f)

    return gspread.authorize(creds)


def _ensure_tab(sh, name: str, headers: list[str]):
    """Return worksheet ``name``, creating it with a header row if it doesn't exist."""
    existing = {ws.title: ws for ws in sh.worksheets()}
    if name in existing:
        return existing[name]
    ws = sh.add_worksheet(title=name, rows=1000, cols=max(len(headers), 26))
    ws.append_row(headers, value_input_option="USER_ENTERED")
    return ws


def _norm(v) -> str:
    """Canonical string for one dedup-key element, safe for a Sheets round-trip.

    Floats/ints  → fixed 8-decimal string (avoids float comparison drift).
    date objects → ISO "YYYY-MM-DD".
    None / "" / "--" → "".
    Other strings → re-parsed as float if possible, else returned verbatim.
    """
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    # float before int because bool is a subclass of int
    if isinstance(v, float):
        return f"{v:.8f}"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{float(v):.8f}"
    s = str(v).strip()
    if not s or s == "--":
        return ""
    try:
        return f"{float(s):.8f}"
    except ValueError:
        return s


def _tx_canonical_key(tx: Transaction) -> tuple[str, ...]:
    """Canonical dedup key for a Transaction, matching ``_row_canonical_key`` on readback."""
    return tuple(_norm(e) for e in tx.dedup_key)


def _row_canonical_key(row: dict[str, str]) -> tuple[str, ...]:
    """Reconstruct canonical dedup key from a Transactions-tab row dict."""
    tx_type = row.get("tx_type", "").strip()
    if tx_type == "INIT_BUY":
        return (
            "INIT",
            _norm(row.get("account_number", "")),
            _norm(row.get("symbol", "")),
            _norm(row.get("trade_date", "")),
            _norm(row.get("quantity", "")),
            _norm(row.get("price", "")),
        )
    return (
        _norm(row.get("trade_date", "")),
        _norm(row.get("settlement_date", "")),
        _norm(row.get("account_number", "")),
        _norm(row.get("tx_type", "")),
        _norm(row.get("symbol", "")),
        _norm(row.get("quantity", "")),
        _norm(row.get("amount", "")),
    )


def _read_tab_rows(
    ws: gspread.Worksheet, keys: list[str] | None = None
) -> list[dict[str, str]]:
    """Return all data rows as dicts. Skips blank rows.

    When ``keys`` is given, each row is keyed by POSITION against ``keys`` — the
    sheet's header row is skipped but otherwise ignored. This decouples the dict
    keys from the displayed header text, so relabeling a tab's header (e.g.
    snake_case → Title Case) cannot change dedup behavior on an already-deployed
    sheet. When ``keys`` is None, the sheet's own header row supplies the keys.
    """
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return []
    header, *data_rows = all_values
    field_keys = keys if keys is not None else header
    result = []
    for row in data_rows:
        padded = row + [""] * (len(field_keys) - len(row))
        d = dict(zip(field_keys, padded))
        if any(v.strip() for v in d.values()):
            result.append(d)
    return result


def _refresh_header(ws, headers: list[str]) -> None:
    """Overwrite row 1 with ``headers``.

    Cosmetic only — keeps append-only tabs (Transactions, Run Log) showing the
    current Title Case labels even though ``_ensure_tab`` writes a header only on
    creation. Correctness never depends on this: reads are position-based.
    Keyword args are required because gspread v5/v6 swap the positional order of
    ``range_name`` and ``values``.
    """
    ws.update(values=[headers], range_name="A1", value_input_option="USER_ENTERED")


def load_existing_transaction_keys(sh) -> set[tuple]:
    """Read Transactions tab; return canonical dedup_key set for all rows."""
    ws_names = {ws.title for ws in sh.worksheets()}
    if TAB_TRANSACTIONS not in ws_names:
        return set()
    ws = sh.worksheet(TAB_TRANSACTIONS)
    return {_row_canonical_key(row) for row in _read_tab_rows(ws, TRANSACTIONS_KEYS)}


def load_existing_source_files(sh) -> set[str]:
    """Return all distinct source_file values from the Transactions tab.

    Used by the runner to skip INIT_BUY rows from already-imported Unrealized files.
    """
    ws_names = {ws.title for ws in sh.worksheets()}
    if TAB_TRANSACTIONS not in ws_names:
        return set()
    ws = sh.worksheet(TAB_TRANSACTIONS)
    return {
        row["source_file"].strip()
        for row in _read_tab_rows(ws, TRANSACTIONS_KEYS)
        if row.get("source_file", "").strip()
    }


def load_recorded_symbols(sh) -> list[str]:
    """Distinct symbols recorded in the Transactions tab (held OR since sold).

    Read position-based against ``TRANSACTIONS_KEYS`` so it's immune to header
    relabels. Returns RAW symbol strings (Merrill form), order-preserving; the
    caller normalizes and drops cash/blank (via ``normalize_all``).
    """
    ws_names = {ws.title for ws in sh.worksheets()}
    if TAB_TRANSACTIONS not in ws_names:
        return []
    ws = sh.worksheet(TAB_TRANSACTIONS)
    out: list[str] = []
    seen: set[str] = set()
    for row in _read_tab_rows(ws, TRANSACTIONS_KEYS):
        sym = row.get("symbol", "").strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def load_watchlist_symbols(sh) -> list[str]:
    """Symbols from the human-editable Watchlist tab (single ``Symbol`` column).

    Ensures the tab exists (created with a ``Symbol`` header if missing — never
    clobbers user rows). Returns RAW symbol strings, order-preserving; the caller
    normalizes and drops cash/blank. Locates the column whose header is ``Symbol``
    (case-insensitive); falls back to the first column.
    """
    ws = _ensure_tab(sh, TAB_WATCHLIST, WATCHLIST_HEADERS)
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return []
    header, *data_rows = all_values
    col = next(
        (i for i, label in enumerate(header) if label.strip().lower() == "symbol"),
        0,
    )
    out: list[str] = []
    seen: set[str] = set()
    for row in data_rows:
        sym = row[col].strip() if col < len(row) else ""
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)
    return out


def write_transactions(
    sh: gspread.Spreadsheet,
    transactions: list[Transaction],
    existing_keys: set[tuple],
) -> int:
    """Append new transactions not already in ``existing_keys``. Returns count added.

    Uses RAW value_input_option so ISO date strings are stored as text, not
    converted to Sheets date serials — which preserves the round-trip format
    used by ``load_existing_transaction_keys``.
    """
    ws = _ensure_tab(sh, TAB_TRANSACTIONS, TRANSACTIONS_HEADERS)
    _refresh_header(ws, TRANSACTIONS_HEADERS)

    new_rows = []
    for tx in transactions:
        if _tx_canonical_key(tx) in existing_keys:
            continue
        new_rows.append([
            tx.trade_date.isoformat(),
            tx.settlement_date.isoformat(),
            tx.status,
            tx.account_number,
            tx.account_registration,
            tx.tx_type,
            tx.description,
            tx.symbol or "",
            "" if tx.quantity is None else tx.quantity,
            "" if tx.price is None else tx.price,
            tx.amount,
            tx.source_file,
        ])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")

    return len(new_rows)


def write_holdings(sh: gspread.Spreadsheet, positions: list[Position]) -> None:
    """Overwrite Holdings tab with current positions and GOOGLEFINANCE price formulas."""
    ws = _ensure_tab(sh, TAB_HOLDINGS, HOLDINGS_HEADERS)
    ws.clear()
    ws.append_row(HOLDINGS_HEADERS, value_input_option="USER_ENTERED")

    today = date.today().isoformat()
    rows = []
    for i, pos in enumerate(sorted(positions, key=lambda p: (p.account_number, p.symbol))):
        row_num = i + 2  # row 1 is the header
        rows.append([
            pos.account_number,                                # A
            pos.account_registration,                          # B
            pos.symbol,                                        # C
            round(pos.quantity, 8),                            # D
            round(pos.avg_cost, 6),                            # E
            round(pos.total_cost_basis, 2),                    # F
            f'=IFERROR(GOOGLEFINANCE(C{row_num},"price"),0)',  # G current_price
            f'=IFERROR(D{row_num}*G{row_num},0)',              # H market_value
            today,                                             # I as_of_date
        ])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_cash(sh: gspread.Spreadsheet, balances: list[CashBalance]) -> None:
    """Overwrite Cash tab with reconstructed vs snapshot balances."""
    ws = _ensure_tab(sh, TAB_CASH, CASH_HEADERS)
    ws.clear()
    ws.append_row(CASH_HEADERS, value_input_option="USER_ENTERED")

    rows = []
    for bal in sorted(balances, key=lambda b: (b.account_number, b.cash_account)):
        owner = ACCOUNT_OWNERS.get(bal.account_number, "")
        rows.append([
            bal.account_number,                                      # A
            bal.account_registration,                               # B
            owner,                                                  # C
            bal.cash_account,                                       # D
            round(bal.reconstructed, 2),                            # E
            "" if bal.snapshot is None else round(bal.snapshot, 2),  # F
            "" if bal.drift is None else round(bal.drift, 2),        # G
            bal.as_of_date.isoformat(),                             # H as_of_date
        ])

    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def write_stock_metrics(
    sh: gspread.Spreadsheet,
    fundamentals: dict[str, StockFundamentals],
    run_date: date,
) -> None:
    """Overwrite Stock Metrics tab with yfinance fundamentals + GOOGLEFINANCE formulas."""
    ws = _ensure_tab(sh, TAB_STOCK_METRICS, STOCK_METRICS_HEADERS)
    ws.clear()
    ws.append_row(STOCK_METRICS_HEADERS, value_input_option="USER_ENTERED")

    rows = []
    for i, (symbol, f) in enumerate(sorted(fundamentals.items())):
        row_num = i + 2
        sym_ref = f"A{row_num}"
        rows.append([
            symbol,                                                # A
            "" if f.pe_ratio is None else f.pe_ratio,             # B
            "" if f.dividend_yield is None else f.dividend_yield,  # C
            "" if f.roe_current is None else f.roe_current,        # D
            "" if f.roe_1y is None else f.roe_1y,                  # E
            "" if f.roe_2y is None else f.roe_2y,                  # F
            "" if f.roe_3y is None else f.roe_3y,                  # G
            "" if f.roe_4y is None else f.roe_4y,                  # H
            "" if f.net_income is None else f.net_income,          # I
            "" if f.book_value is None else f.book_value,          # J
            f'=IFERROR(GOOGLEFINANCE({sym_ref},"price"),"N/A")',   # K current_price
            run_date.isoformat(),                                  # L as_of_date
        ])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_price_history(
    sh: gspread.Spreadsheet,
    histories: dict[str, PriceHistory],
) -> None:
    """Overwrite the Price History tab with 5-yr weekly closes (one column per symbol).

    Builds a unified, sorted Date axis (the union of every symbol's dates) so symbols
    with different history lengths still align — blanks fill the gaps. Date is column
    A; symbols are the remaining columns, sorted alphabetically (matching Stock Metrics).
    """
    ws = _ensure_tab(sh, TAB_PRICE_HISTORY, ["Date"])
    ws.clear()

    symbols = sorted(histories)
    by_symbol: dict[str, dict[str, float]] = {}
    all_dates: set[str] = set()
    for sym in symbols:
        h = histories[sym]
        pairs = dict(zip(h.dates, h.closes))
        by_symbol[sym] = pairs
        all_dates.update(pairs)

    ws.append_row(["Date", *symbols], value_input_option="USER_ENTERED")

    rows = []
    for d in sorted(all_dates):
        row = [d]
        for sym in symbols:
            close = by_symbol[sym].get(d)
            row.append("" if close is None else close)
        rows.append(row)

    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def write_run_log(sh: gspread.Spreadsheet, entry: RunLogEntry) -> None:
    """Append one row to the Run Log tab."""
    ws = _ensure_tab(sh, TAB_RUN_LOG, RUN_LOG_HEADERS)
    _refresh_header(ws, RUN_LOG_HEADERS)
    ws.append_row(
        [
            entry.run_timestamp,
            entry.files_processed,
            entry.init_rows_added,
            entry.transactions_added,
            entry.accounts_skipped,
            entry.errors,
            entry.holdings_changed,
            entry.cash_reconciliation,
            entry.duration_sec,
            entry.notes,
        ],
        value_input_option="RAW",
    )
