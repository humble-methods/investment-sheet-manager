"""Google Sheets output: Transactions, Holdings, Cash, Stock Metrics, Run Log."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from portfolio.config import ACCOUNT_OWNERS
from portfolio.metrics.pricing import anniversary, close_on_or_before
from portfolio.models import CashBalance, Position, RunLogEntry, Transaction

if TYPE_CHECKING:
    from portfolio.market.yfinance_client import PriceHistory, StockFundamentals
    from portfolio.metrics.performance import SymbolPerformance, YearPerformance

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
TAB_PERFORMANCE = "Performance"
TAB_PERFORMANCE_BY_YEAR = "Performance By Year"
TAB_PERFORMANCE_COMPARE = "Performance Compare"

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
# A=Symbol B=P/E Ratio C=Dividend Yield D=ROE (Current) E..N=ROE (1Y..10Y Ago)
# O=Net Income P=Book Value Q=Current Price R=As Of Date
# The sheet is provisioned for 10 ROE years; yfinance yields ~4 per fetch, so the
# later columns fill in over time as the cache accumulates (see yfinance_client).
# Columns are RELATIVE ("N Y Ago"): at write time each ticker's calendar-year ROE
# (cache key) is placed under column k where year == run_year - k.
# 5-year weekly closes live on the separate Price History tab (fetched by Python).
ROE_YEARS_BACK = 10
_ROE_YEAR_HEADERS = [f"ROE ({k}Y Ago)" for k in range(1, ROE_YEARS_BACK + 1)]
STOCK_METRICS_HEADERS = [
    "Symbol", "P/E Ratio", "Dividend Yield", "ROE (Current)",
    *_ROE_YEAR_HEADERS,
    "Net Income", "Book Value", "Current Price", "As Of Date",
]
RUN_LOG_HEADERS = [
    "Run Timestamp", "Files Processed", "Init Rows Added", "Transactions Added",
    "Accounts Skipped", "Errors", "Holdings Changed", "Cash Reconciliation",
    "Duration (Sec)", "Notes",
]
# Human-maintained intake tab: one ticker per row under "Symbol".
WATCHLIST_HEADERS = ["Symbol"]
# Price History: one row per symbol; columns are anniversary snapshots back from
# today (the latest weekly close on/before each anniversary date).
PRICE_HISTORY_HEADERS = [
    "Symbol", "Today", "1Y Ago", "2Y Ago", "3Y Ago", "4Y Ago", "5Y Ago",
]
# Performance: one row per symbol + a PORTFOLIO row. Returns are fractions (0..1).
PERFORMANCE_HEADERS = [
    "Symbol", "First Held", "Current Value", "Cost Basis",
    "Lifetime Total XIRR", "Lifetime Price XIRR", "Income Contribution", "As Of Date",
]
# Per-year (long format): one row per (symbol, year). Returns are fractions (0..1).
PERFORMANCE_BY_YEAR_HEADERS = [
    "Symbol", "Year", "Begin Value", "End Value", "Net Flows", "Dividends",
    "Total Return", "Price Return", "As Of Date",
]
# Performance Compare: interactive side-by-side cards, scaffolded once (set-up-once
# like Watchlist). Fixed single-select dropdown slots feed live lookups into the
# Performance / Performance By Year data tabs. Year rows = current year + 5 prior,
# matching the 5-yr price-history window.
PERFORMANCE_COMPARE_SLOTS = 5
PERFORMANCE_COMPARE_YEARS = 6


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
        # Project the calendar-year-keyed ROE cache onto relative columns:
        # "N Y Ago" == fiscal year (run_year - N). Blank where no data yet.
        roe_cols = []
        for k in range(1, ROE_YEARS_BACK + 1):
            value = f.roe_by_year.get(str(run_date.year - k))
            roe_cols.append("" if value is None else value)
        rows.append([
            symbol,                                                # A
            "" if f.pe_ratio is None else f.pe_ratio,             # B
            "" if f.dividend_yield is None else f.dividend_yield,  # C
            "" if f.roe_current is None else f.roe_current,        # D
            *roe_cols,                                             # E..N (1Y..10Y Ago)
            "" if f.net_income is None else f.net_income,          # O
            "" if f.book_value is None else f.book_value,          # P
            f'=IFERROR(GOOGLEFINANCE({sym_ref},"price"),"N/A")',   # Q current_price
            run_date.isoformat(),                                  # R as_of_date
        ])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_price_history(
    sh: gspread.Spreadsheet,
    histories: dict[str, PriceHistory],
    today: date | None = None,
) -> None:
    """Overwrite the Price History tab: one row per symbol, columns are anniversary
    snapshots back from ``today`` (Today, 1Y Ago … 5Y Ago).

    Each value is the latest weekly close on/before that anniversary date, so it may be
    up to ~a week stale. The fuller weekly series stays in the Drive cache (and feeds
    the Performance year-boundary math); this tab is the slimmed human-readable view.
    Symbols are rows, sorted alphabetically (matching Stock Metrics).
    """
    today = today or date.today()
    ws = _ensure_tab(sh, TAB_PRICE_HISTORY, PRICE_HISTORY_HEADERS)
    ws.clear()
    ws.append_row(PRICE_HISTORY_HEADERS, value_input_option="USER_ENTERED")

    anchors = [anniversary(today, k) for k in range(len(PRICE_HISTORY_HEADERS) - 1)]
    rows = []
    for sym in sorted(histories):
        h = histories[sym]
        row = [sym]
        for a in anchors:
            c = close_on_or_before(h, a)
            row.append("" if c is None else round(c, 4))
        rows.append(row)

    if rows:
        ws.append_rows(rows, value_input_option="RAW")


def write_performance(
    sh: gspread.Spreadsheet,
    summaries: list[SymbolPerformance],
    run_date: date,
) -> None:
    """Overwrite the Performance tab: lifetime annualized XIRR (total + price) per
    symbol plus a pooled PORTFOLIO row. Returns are fractions (0..1) — format as %.
    """
    ws = _ensure_tab(sh, TAB_PERFORMANCE, PERFORMANCE_HEADERS)
    ws.clear()
    ws.append_row(PERFORMANCE_HEADERS, value_input_option="USER_ENTERED")

    today = run_date.isoformat()
    out = []
    for s in summaries:
        out.append([
            s.symbol,                                                                  # A
            "" if s.first_held is None else s.first_held.isoformat(),                  # B
            round(s.current_value, 2),                                                 # C
            round(s.cost_basis, 2),                                                    # D
            "" if s.lifetime_total_xirr is None else round(s.lifetime_total_xirr, 6),  # E
            "" if s.lifetime_price_xirr is None else round(s.lifetime_price_xirr, 6),  # F
            "" if s.income_contribution is None else round(s.income_contribution, 6),  # G
            today,                                                                     # H
        ])

    if out:
        ws.append_rows(out, value_input_option="RAW")


def write_performance_by_year(
    sh: gspread.Spreadsheet,
    yearly: list[YearPerformance],
    run_date: date,
) -> None:
    """Overwrite the Performance By Year tab: one row per (symbol, year) with
    non-annualized Modified Dietz total + price returns. Returns are fractions.
    """
    ws = _ensure_tab(sh, TAB_PERFORMANCE_BY_YEAR, PERFORMANCE_BY_YEAR_HEADERS)
    ws.clear()
    ws.append_row(PERFORMANCE_BY_YEAR_HEADERS, value_input_option="USER_ENTERED")

    today = run_date.isoformat()
    out = []
    for y in yearly:
        out.append([
            y.symbol,                                                    # A
            y.year,                                                      # B
            round(y.begin_value, 2),                                     # C
            round(y.end_value, 2),                                       # D
            round(y.net_flows, 2),                                       # E
            round(y.dividends, 2),                                       # F
            "" if y.total_return is None else round(y.total_return, 6),  # G
            "" if y.price_return is None else round(y.price_return, 6),  # H
            today,                                                       # I
        ])

    if out:
        ws.append_rows(out, value_input_option="RAW")


_COMPARE_LIFETIME_ROWS = [
    ("First Held", 2),
    ("Current Value", 3),
    ("Cost Basis", 4),
    ("Lifetime Total XIRR", 5),
    ("Lifetime Price XIRR", 6),
    ("Income Contribution", 7),
]


def _compare_lifetime_formula(col: str, perf_col: int) -> str:
    """VLOOKUP a lifetime metric (Performance column ``perf_col``) for the symbol
    picked in ``{col}$1``; blank when the slot is empty or the symbol is missing."""
    return (
        f'=IF({col}$1="","",'
        f'IFERROR(VLOOKUP({col}$1,{TAB_PERFORMANCE}!$A:$H,{perf_col},FALSE),""))'
    )


def _compare_year_formula(col: str, years_back: int, pby_col: str) -> str:
    """INDEX/MATCH a (symbol, year) return from Performance By Year. ``years_back`` is
    relative to TODAY() so the rows stay current with no rewrite; ``pby_col`` is the
    data column letter (G total return, H price return)."""
    pby = f"'{TAB_PERFORMANCE_BY_YEAR}'"
    return (
        f'=IF({col}$1="","",'
        f'IFERROR(INDEX({pby}!${pby_col}$2:${pby_col},'
        f'MATCH({col}$1&"|"&(YEAR(TODAY())-{years_back}),'
        f'ARRAYFORMULA({pby}!$A$2:$A&"|"&{pby}!$B$2:$B),0)),""))'
    )


def _performance_compare_grid(slots: int, years: int) -> list[list[str]]:
    """Label column + per-slot lookup formulas. Symbols are NOT pre-filled — the user
    picks them via the row-1 dropdowns; year labels/rows use TODAY() so they self-update."""
    letters = [chr(ord("B") + i) for i in range(slots)]
    grid: list[list[str]] = [["Symbol →", *[""] * slots]]
    for label, perf_col in _COMPARE_LIFETIME_ROWS:
        grid.append([label, *[_compare_lifetime_formula(c, perf_col) for c in letters]])
    grid.append(["", *[""] * slots])  # spacer between lifetime + per-year blocks
    for k in range(years):
        for suffix, pby_col in (("Total", "G"), ("Price", "H")):
            label = f'=TEXT(YEAR(TODAY())-{k},"0")&" {suffix}"'
            grid.append([label, *[_compare_year_formula(c, k, pby_col) for c in letters]])
    return grid


def write_performance_compare(
    sh: gspread.Spreadsheet,
    slots: int = PERFORMANCE_COMPARE_SLOTS,
    years: int = PERFORMANCE_COMPARE_YEARS,
) -> None:
    """Scaffold the interactive Performance Compare tab (set-up-once).

    Fixed single-select dropdown slots in row 1 (sourced from the Performance tab's
    symbol column) each drive one side-by-side card of live lookups into the
    Performance / Performance By Year data tabs. Every dynamic value is a formula
    (years use TODAY()), so the view self-refreshes as those tabs update — we therefore
    build it only when the tab is absent, never clobbering the user's slot picks.
    """
    if TAB_PERFORMANCE_COMPARE in {ws.title for ws in sh.worksheets()}:
        return
    grid = _performance_compare_grid(slots, years)
    ws = sh.add_worksheet(
        title=TAB_PERFORMANCE_COMPARE,
        rows=max(len(grid) + 5, 26),
        cols=max(slots + 1, 26),
    )
    ws.update(values=grid, range_name="A1", value_input_option="USER_ENTERED")
    _apply_compare_dropdowns(sh, ws, slots)


def _apply_compare_dropdowns(sh, ws, slots: int) -> None:
    """Put a single-select dropdown on each row-1 slot (B1…), sourced from the
    Performance symbol column, and freeze the header row + label column. Uses the raw
    Sheets API via gspread's ``batch_update`` — gspread has no data-validation helper."""
    source = f"={TAB_PERFORMANCE}!$A$2:$A"
    sh.batch_update({
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": 0, "endRowIndex": 1,
                        "startColumnIndex": 1, "endColumnIndex": 1 + slots,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_RANGE",
                            "values": [{"userEnteredValue": source}],
                        },
                        "showCustomUi": True,
                        "strict": False,
                    },
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": ws.id,
                        "gridProperties": {
                            "frozenRowCount": 1,
                            "frozenColumnCount": 1,
                        },
                    },
                    "fields": (
                        "gridProperties.frozenRowCount,"
                        "gridProperties.frozenColumnCount"
                    ),
                }
            },
        ]
    })


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
