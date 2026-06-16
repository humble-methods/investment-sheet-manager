"""Google Sheets output: Transactions, Holdings, Cash, Stock Metrics, Run Log."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from portfolio.config import ACCOUNT_OWNERS
from portfolio.models import CashBalance, Position, RunLogEntry, Transaction

if TYPE_CHECKING:
    from portfolio.market.yfinance_client import StockFundamentals

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

TRANSACTIONS_HEADERS = [
    "trade_date", "settlement_date", "status", "account_number",
    "account_registration", "tx_type", "description", "symbol",
    "quantity", "price", "amount", "source_file",
]
# A=as_of_date B=account_number C=account_registration D=symbol
# E=quantity F=avg_cost G=cost_basis H=current_price I=market_value
HOLDINGS_HEADERS = [
    "as_of_date", "account_number", "account_registration", "symbol",
    "quantity", "avg_cost", "cost_basis", "current_price", "market_value",
]
CASH_HEADERS = [
    "as_of_date", "account_number", "account_registration", "owner",
    "cash_account", "reconstructed", "snapshot", "drift",
]
# A=as_of_date B=symbol C=pe_ratio D=dividend_yield E=roe_current
# F=roe_1y G=roe_2y H=roe_3y I=roe_4y J=net_income K=book_value
# L=current_price M=high_52wk N=low_52wk
STOCK_METRICS_HEADERS = [
    "as_of_date", "symbol", "pe_ratio", "dividend_yield",
    "roe_current", "roe_1y", "roe_2y", "roe_3y", "roe_4y",
    "net_income", "book_value", "current_price", "high_52wk", "low_52wk",
]
RUN_LOG_HEADERS = [
    "run_timestamp", "files_processed", "init_rows_added", "transactions_added",
    "accounts_skipped", "errors", "holdings_changed", "cash_reconciliation",
    "duration_sec", "notes",
]


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


def _read_tab_rows(ws: gspread.Worksheet) -> list[dict[str, str]]:
    """Return all data rows as dicts (header row → keys). Skips blank rows."""
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return []
    headers, *data_rows = all_values
    result = []
    for row in data_rows:
        padded = row + [""] * (len(headers) - len(row))
        d = dict(zip(headers, padded))
        if any(v.strip() for v in d.values()):
            result.append(d)
    return result


def load_existing_transaction_keys(sh) -> set[tuple]:
    """Read Transactions tab; return canonical dedup_key set for all rows."""
    ws_names = {ws.title for ws in sh.worksheets()}
    if TAB_TRANSACTIONS not in ws_names:
        return set()
    ws = sh.worksheet(TAB_TRANSACTIONS)
    return {_row_canonical_key(row) for row in _read_tab_rows(ws)}


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
        for row in _read_tab_rows(ws)
        if row.get("source_file", "").strip()
    }


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
    """Overwrite Holdings tab with current positions and GOOGFINANCE price formulas."""
    ws = _ensure_tab(sh, TAB_HOLDINGS, HOLDINGS_HEADERS)
    ws.clear()
    ws.append_row(HOLDINGS_HEADERS, value_input_option="USER_ENTERED")

    today = date.today().isoformat()
    rows = []
    for i, pos in enumerate(sorted(positions, key=lambda p: (p.account_number, p.symbol))):
        row_num = i + 2  # row 1 is the header
        rows.append([
            today,
            pos.account_number,
            pos.account_registration,
            pos.symbol,
            round(pos.quantity, 8),
            round(pos.avg_cost, 6),
            round(pos.total_cost_basis, 2),
            f'=IFERROR(GOOGFINANCE(D{row_num},"price"),0)',
            f'=IFERROR(E{row_num}*H{row_num},0)',
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
            bal.as_of_date.isoformat(),
            bal.account_number,
            bal.account_registration,
            owner,
            bal.cash_account,
            round(bal.reconstructed, 2),
            "" if bal.snapshot is None else round(bal.snapshot, 2),
            "" if bal.drift is None else round(bal.drift, 2),
        ])

    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def write_stock_metrics(
    sh: gspread.Spreadsheet,
    fundamentals: dict[str, StockFundamentals],
    run_date: date,
) -> None:
    """Overwrite Stock Metrics tab with yfinance fundamentals + GOOGFINANCE formulas."""
    ws = _ensure_tab(sh, TAB_STOCK_METRICS, STOCK_METRICS_HEADERS)
    ws.clear()
    ws.append_row(STOCK_METRICS_HEADERS, value_input_option="USER_ENTERED")

    rows = []
    for i, (symbol, f) in enumerate(sorted(fundamentals.items())):
        row_num = i + 2
        sym_ref = f"B{row_num}"
        rows.append([
            run_date.isoformat(),
            symbol,
            "" if f.pe_ratio is None else f.pe_ratio,
            "" if f.dividend_yield is None else f.dividend_yield,
            "" if f.roe_current is None else f.roe_current,
            "" if f.roe_1y is None else f.roe_1y,
            "" if f.roe_2y is None else f.roe_2y,
            "" if f.roe_3y is None else f.roe_3y,
            "" if f.roe_4y is None else f.roe_4y,
            "" if f.net_income is None else f.net_income,
            "" if f.book_value is None else f.book_value,
            f'=IFERROR(GOOGFINANCE({sym_ref},"price"),"N/A")',
            f'=IFERROR(GOOGFINANCE({sym_ref},"high52"),"N/A")',
            f'=IFERROR(GOOGFINANCE({sym_ref},"low52"),"N/A")',
        ])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_run_log(sh: gspread.Spreadsheet, entry: RunLogEntry) -> None:
    """Append one row to the Run Log tab."""
    ws = _ensure_tab(sh, TAB_RUN_LOG, RUN_LOG_HEADERS)
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
