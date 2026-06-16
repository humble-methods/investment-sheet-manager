# Build Plan — Investment Sheet Manager

## Overview

Eight sequential phases. Each phase is independently testable before moving to the next. Do not start Phase N+1 until Phase N passes its validation.

**Guiding constraint:** No web app, no database, no scheduler, no complex auth until core pipeline is solid.

**GitHub:** `https://github.com/humble-methods/investment-sheet-manager`
**Drive folder ID:** `<DRIVE_ROOT_FOLDER_ID>`

---

## Data-Validated Findings & Locked Decisions

These were confirmed against real Merrill exports (activity 05/2026, holdings/unrealized/realized 02/2026) and resolved with the user. They override anything looser elsewhere in this plan.

### Account model (broader than originally assumed)
There are many accounts, not one CMA + one Roth. Accounts observed in the **snapshot samples** (holdings/unrealized 02/2026):
- **CMA-Edge:** `11A-00001`, `11A-00002`, `11A-00003`, `11A-00005`, `11A-00006`
- **Roth IRA-Edge:** `22B-00001`

`account_registration` is **not** unique (five accounts share `CMA-Edge`). **`account_number` is the only canonical key.** Account numbers can differ by a single character (e.g. `11A-00003` vs `11A-00004`) — treat as exact strings, never normalize/transform.

> Note: the 05/2026 activity sample was a *separate, personal* export provided only to show the column layout, so its account numbers (`11A-00004`, `22B-00002`) are **not** part of the snapshot roster and are **not** evidence of an onboarding event. The roster is nonetheless treated as **dynamic** — it is discovered at runtime from `account_state.json`, never hardcoded — and the skip-and-flag rule below is the general safeguard for any account that appears in activity without a bootstrap.

### Per-account bootstrap + init date (Decisions 1 & 3)
- Each account is bootstrapped from its **first Unrealized intake**, and that file's **COB Date becomes the account's permanent init/cutoff date** (e.g. `1/30/2026`). Stored once, persistently.
- **Cutoff rule:** when replaying, **ignore any activity transaction with `trade_date <= account_init_date`** — those positions are already baked into the bootstrap snapshot. This prevents double-counting the window between the activity file's start and the snapshot COB.
- Init dates are **operational state, discovered at runtime**, not hardcoded config. Persist them in Drive as `cache/account_state.json` (`{account_number: {init_date, bootstrap_source_file}}`). Set automatically the first time an Unrealized file is seen for a new account.

### Un-bootstrapped accounts → skip + flag (Decision 2)
If the engine encounters activity for an account with **no bootstrap snapshot yet**, it must **skip that account's transactions entirely and flag it in the Run Log** — never crash, never partially process. A new account requires an **Unrealized intake AND a Holdings intake** before its activity is processed (Unrealized → equity lots + init date; Holdings → cash init, see below). Other accounts in the same run process normally.

### Cash tracking: reconstruct + reconcile (Decision 1)
- The ML cash sweep (`990156937`) is the CMA cash account; **IIAXX** is the Roth cash/money-market account. Both are ~$1 NAV dollar-denominated positions.
- **Critical:** the cash sweep is **absent from the Unrealized CSV** — it only appears in **Holdings**. So **cash must be bootstrapped from the Holdings snapshot**, not Unrealized. → A Holdings CSV is required at onboarding alongside the Unrealized CSV.
- **Reconstruct** a running cash balance per account by applying activity deltas to the bootstrap, then **reconcile** against the Holdings snapshot's sweep/IIAXX quantity whenever a Holdings CSV is present; **flag drift** in the Run Log.
- ⚠️ **Double-count hazard to validate in Phase 3:** the sweep `Deposit`/`Withdrawal` rows are the *netted settlement* of trades (e.g. 6/3 trades net ≈ +$41,414; a $41,412 sweep deposit lands 6/5). Summing both trade amounts *and* sweep rows double-counts. The reconstruction algorithm must be validated against a real multi-month activity set before trusting it (see Phase 3). External contributions vs. internal sweeps are **indistinguishable** from the activity CSV alone, so "cash balance" = total sweep balance; contribution-vs-return attribution is **out of scope**.

### Parsing gotchas confirmed in real data (Phase 2)
- **Trailing whitespace in headers and values:** the column is literally `"Description 1 "` and values are `"Purchase "`, `"Sale "`. **`.strip()` every Type / Description 1 value before mapping** or all trades fall through to UNKNOWN.
- **Two Description-2 boilerplate variants:** some rows lead with `ACTUAL PRICES, REMUNERATION… UPON REQUEST. CLIENT ENTERED…`, others go straight to `CLIENT ENTERED…`. Strip from **whichever marker appears first**.
- **INIT_BUY duplicate-lot bug:** a single account legitimately holds **byte-identical lots** (e.g. AXP `11A-00003`: two lots of 5 sh @ $164.35 on 11/26/2021). The per-lot dedup key would wrongly collapse them. **Do NOT inter-dedup INIT rows within a bootstrap file** — every Unrealized row is a distinct lot. Guard only against re-importing the *same* file twice (by `source_file`). (Contrast Decision 3 below for *activity* dupes.)

### Activity duplicate handling (Decision 3, original Q3)
Identical *activity* fills (same date/account/symbol/qty/amount within a settled file) are **flagged as an error for human review**, not silently merged and not auto-deduped-away — Merrill platform-split fills would otherwise be lost. This differs from INIT_BUY, where identical rows are kept.

### Realized CSV: unused for MVP (confirmed)
Activity is a strict superset for the ongoing flow (BUY/SELL **plus** dividends, interest, ADR fees, tax withholding, cash sweeps). Realized only carries matched closed lots and uses internal security codes (not tickers). Any realized P&L can be computed by the FIFO engine. **Realized CSV is not ingested.**

### Corporate actions: out of scope for MVP (confirmed)
Splits / renames / mergers are handled by **manual intervention** — the user re-pulls and adjusts position files when one occurs. The engine should **flag unmapped `(Type, Description 1)` combos** as `UNKNOWN` rather than attempt to handle them.

---

## Phase 1 — Project Foundation
**Goal:** Empty repo becomes a valid Python package with config, models, and tooling in place.

### Deliverables
- `requirements.txt`
- `.gitignore`
- `portfolio/__init__.py`
- `portfolio/config.py`
- `portfolio/models.py`
- `tests/__init__.py`
- `tests/sample_data/` — redacted sample CSVs

### `portfolio/config.py`
```python
# Ticker normalization: Merrill symbol → yfinance symbol
SYMBOL_OVERRIDES: dict[str, str] = {
    "BRKB": "BRK-B",
}

# Skip these from equity processing (cash/money market)
CASH_CUSIPS: set[str] = {"990156937"}
CASH_SYMBOLS: set[str] = {"IIAXX"}

# Google Drive — folder IDs are secrets, NOT committed. Supply at runtime via
# environment (Colab: google.colab.userdata; local: .env / shell env).
# Real values are kept in the untracked .secrets.local.md.
import os
DRIVE_ROOT_FOLDER_ID: str      = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
DRIVE_UPLOAD_FOLDER_ID: str    = os.environ.get("DRIVE_UPLOAD_FOLDER_ID", "")
DRIVE_PROCESSED_FOLDER_ID: str = os.environ.get("DRIVE_PROCESSED_FOLDER_ID", "")
DRIVE_FAILED_FOLDER_ID: str    = os.environ.get("DRIVE_FAILED_FOLDER_ID", "")
DRIVE_CACHE_FOLDER_ID: str     = os.environ.get("DRIVE_CACHE_FOLDER_ID", "")

# Google Sheets — also treat the spreadsheet ID as a secret; supply via env.
SPREADSHEET_ID: str = os.environ.get("SPREADSHEET_ID", "")

# yfinance cache TTL
CACHE_TTL_HOURS: int = 24

# Cash / money-market identifiers (per-account cash accounts, ~$1 NAV)
CASH_SWEEP_CUSIP: str = "990156937"  # ML DIRECT DEPOSIT PROGRM (CMA cash)
CASH_MMKT_SYMBOL: str = "IIAXX"      # BofA RASP (Roth cash)

# Known account roster (informational; account_number is the canonical key).
# account_registration is NOT unique — multiple accounts share "CMA-Edge".
# Optional owner labels help the shared (intentionally cross-visible) sheet stay readable.
ACCOUNT_OWNERS: dict[str, str] = {
    # "11A-00001": "Owner A",  # fill in as confirmed
    # "22B-00002": "Owner B",
}

# Per-account init/cutoff dates are NOT hardcoded here. They are discovered at
# runtime from the COB Date of each account's FIRST Unrealized intake and
# persisted to Drive as cache/account_state.json. config may hold overrides only.
ACCOUNT_INIT_DATE_OVERRIDES: dict[str, str] = {}   # account_number -> "M/D/YYYY"
ACCOUNT_STATE_FILENAME: str = "account_state.json"  # lives in DRIVE_CACHE_FOLDER_ID
```

### `account_state.json` (Drive `cache/` — operational state, not config)
```json
{
  "11A-00003": {"init_date": "1/30/2026", "bootstrap_source_file": "Unrealized_AllAccounts_022026.csv"},
  "22B-00001": {"init_date": "1/30/2026", "bootstrap_source_file": "Unrealized_AllAccounts_022026.csv"}
}
```
- Written the first time an Unrealized file introduces a new account; never overwritten afterward (init date is permanent).
- Read at the start of every run to drive the activity cutoff and the skip-and-flag check for un-bootstrapped accounts.

### `portfolio/models.py`
```python
from dataclasses import dataclass, field
from datetime import date

@dataclass
class Transaction:
    trade_date: date
    settlement_date: date
    status: str             # always "Settled" (Pending rows are skipped)
    account_number: str
    account_registration: str  # "CMA-Edge" | "Roth IRA-Edge"
    tx_type: str            # BUY | SELL | INIT_BUY | DIVIDEND | INTEREST |
                            # CASH_IN | CASH_OUT | ADR_FEE | TAX_WITHHOLDING | REINVEST
    description: str
    symbol: str | None      # None for cash transactions
    quantity: float | None  # None for dividends/interest
    price: float | None     # None for dividends/interest
    amount: float           # Negative = money left account; positive = money arrived
    source_file: str        # filename, or "INIT:<filename>" for bootstrap rows

    @property
    def dedup_key(self) -> tuple:
        if self.tx_type == "INIT_BUY":
            return ("INIT", self.account_number, self.symbol,
                    self.trade_date, self.quantity, self.price)
        return (self.trade_date, self.settlement_date, self.account_number,
                self.tx_type, self.symbol, self.quantity, self.amount)

@dataclass
class Lot:
    account_number: str
    symbol: str
    acquisition_date: date
    quantity: float
    unit_cost: float

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.unit_cost

@dataclass
class Position:
    account_number: str
    account_registration: str
    symbol: str
    quantity: float
    lots: list[Lot] = field(default_factory=list)

    @property
    def total_cost_basis(self) -> float:
        return sum(lot.cost_basis for lot in self.lots)

    @property
    def avg_cost(self) -> float:
        return self.total_cost_basis / self.quantity if self.quantity else 0.0

@dataclass
class CashBalance:
    account_number: str
    account_registration: str
    cash_account: str        # "990156937" (CMA sweep) | "IIAXX" (Roth mmkt)
    reconstructed: float     # running balance replayed from bootstrap + activity
    snapshot: float | None   # from latest Holdings CSV, if present this run
    as_of_date: date

    @property
    def drift(self) -> float | None:
        if self.snapshot is None:
            return None
        return round(self.reconstructed - self.snapshot, 2)

@dataclass
class RunLogEntry:
    run_timestamp: str
    files_processed: int
    init_rows_added: int
    transactions_added: int
    accounts_skipped: str    # un-bootstrapped accounts deferred this run
    errors: str
    holdings_changed: str
    cash_reconciliation: str # per-account reconstructed vs snapshot drift
    duration_sec: float
    notes: str
```

### Validation
```bash
python -c "from portfolio.config import SYMBOL_OVERRIDES; print('config OK')"
python -c "from portfolio.models import Transaction, Lot, Position; print('models OK')"
```

---

## Phase 2 — CSV Parsing
**Goal:** Parse all four Merrill CSV types into clean Python objects.

### Deliverables
- `portfolio/parsers/utils.py`
- `portfolio/parsers/activity_parser.py`
- `portfolio/parsers/unrealized_parser.py`
- `portfolio/parsers/holdings_parser.py`
- `tests/test_activity_parser.py`
- `tests/test_unrealized_parser.py`

### `utils.py`
```python
def parse_amount(value: str) -> float | None:
    """
    ""  or "--"     → None
    "(3,211.38)"    → -3211.38
    "3,211.38"      → 3211.38
    "128.46"        → 128.46
    "19"            → 19.0
    """

def parse_date(value: str) -> date:
    """M/D/YYYY → date"""

def clean_symbol(raw: str, cusip: str = "") -> str | None:
    """
    Returns normalized yfinance-compatible symbol, or None for cash positions.
    Applies SYMBOL_OVERRIDES. Returns None if cusip in CASH_CUSIPS or raw in CASH_SYMBOLS.
    """

def clean_description(raw: str) -> str:
    """
    Strip Merrill boilerplate from Description 2 field.
    Cut from the FIRST of these markers, whichever appears earliest:
      - "ACTUAL PRICES, REMUNERATION"
      - "CLIENT ENTERED."
    Both variants occur in real data; some rows have ACTUAL PRICES *before*
    CLIENT ENTERED. Return everything before the earliest marker, stripped.
    """

def strip_field(value: str) -> str:
    """
    Trim surrounding whitespace. REQUIRED on Type and Description 1 before any
    TX_TYPE_MAP lookup — real exports ship "Purchase ", "Sale " (trailing space)
    and the header is literally "Description 1 ".
    """

def detect_csv_type(filename: str) -> str:
    """
    "PendingAndSettledActivity_*" → "activity"
    "Holdings_*"                  → "holdings"
    "Realized_*"                  → "realized"
    "Unrealized_*"                → "unrealized"
    else                          → "unknown"
    """
```

### `activity_parser.py`
```python
TX_TYPE_MAP: dict[tuple[str, str], str] = {
    ("SecurityTransactions", "Purchase"):                  "BUY",
    ("SecurityTransactions", "Sale"):                      "SELL",
    ("SecurityTransactions", "Interest"):                  "REINVEST",
    ("DividendAndInterest",  "Dividend"):                  "DIVIDEND",
    ("DividendAndInterest",  "Foreign Dividend"):          "DIVIDEND",
    ("DividendAndInterest",  "Bank Interest"):             "INTEREST",
    ("Other",                "Deposit"):                   "CASH_IN",
    ("Other",                "Withdrawal"):                "CASH_OUT",
    ("Other",                "Depository Bank (ADR) Fee"): "ADR_FEE",
    ("Other",                "Foreign Tax Withholding"):   "TAX_WITHHOLDING",
}

def parse_activity_csv(filepath: str | Path) -> list[Transaction]:
    """
    - strip_field() Type and Description 1 BEFORE TX_TYPE_MAP lookup (trailing spaces!)
    - Skip rows where Pending/Settled == "Pending"
    - Cash sweep / mmkt rows (990156937, IIAXX): keep as transactions with
      symbol=None for cash-ledger reconstruction, but they create NO equity lots.
      (Equity FIFO ignores them; cash reconstruction uses them — see Phase 3.)
    - Strip Description 2 boilerplate (first marker wins)
    - Quantity: negative in Merrill CSV for sales (keep as-is; FIFO engine uses sign)
    - Unknown (Type, Description 1) after strip → tx_type="UNKNOWN", log, do not crash
    - source_file = filepath.name
    """
```

**Note on quantity sign:** SELL rows in the activity CSV have negative quantity (e.g., `-84`). Keep that as-is in the Transaction. The FIFO engine interprets negative quantity as a sell.

### `unrealized_parser.py`
```python
def parse_unrealized_csv(filepath: str | Path) -> list[Transaction]:
    """
    Convert each lot row into an INIT_BUY Transaction.
    - Skip cash rows (CUSIP 990156937 or symbol IIAXX)
    - tx_type = "INIT_BUY"
    - trade_date = settlement_date = Acquisition Date
    - quantity = Quantity (positive float)
    - price = Unit Cost
    - amount = -(Cost Basis)   [negative: money left account on purchase]
    - source_file = "INIT:<filename>"
    - description = Security Description

    IMPORTANT — every row is a distinct lot. Do NOT dedup rows against each
    other within the file: identical (account, symbol, date, qty, price) lots
    are real and common (e.g. AXP 11A-00003 has two 5 sh @ $164.35 / 11-26-2021).
    Re-import protection is handled at the sheet layer by source_file identity,
    not by collapsing rows here.

    Also records each new account's COB Date as its init_date in
    account_state.json (the first Unrealized intake defines the cutoff).
    """
```

### `holdings_parser.py`
```python
def parse_holdings_csv(filepath: str | Path) -> tuple[
    dict[tuple[str, str], float],   # equity: {(account, symbol): quantity}
    dict[str, float],               # cash:   {account: sweep/mmkt $ balance}
    dict[str, str],                 # registrations: {account: account_registration}
]:
    """
    Equity map → verification of computed holdings (skips cash rows).
    Cash map → bootstrap + reconciliation source for cash balances. The cash
      sweep (990156937) and IIAXX appear ONLY here, not in Unrealized, so the
      Holdings CSV is the authoritative cash source. Value/Quantity is dollars.
    Registrations map → authoritative per-account registration for EVERY account
      (equity + cash). A Roth held only as IIAXX cash has no equity/INIT_BUY
      transaction, so a transaction-derived registration map would miss it and
      mis-label it as a CMA sweep account during cash reconciliation.
    """
```

### Tests
```python
# test_activity_parser.py
- Parse sample activity CSV → correct row count (only Settled rows)
- BUY: quantity positive, amount negative
- SELL: quantity negative, amount positive
- Dividend: quantity None, price None, amount positive
- Deposit (cash): symbol None (skipped from equity), tx_type CASH_IN
- parse_amount("(3,211.38)") == -3211.38
- parse_amount("--") is None
- parse_amount("19") == 19.0
- clean_symbol("BRKB") == "BRK-B"
- clean_symbol("--", cusip="990156937") is None
- Fractional quantity: COF with 30.576 shares parses correctly
- Pending row is skipped

# test_unrealized_parser.py
- Each lot row produces one INIT_BUY Transaction
- IIAXX row is skipped
- amount == -(quantity * unit_cost) for each row
- source_file starts with "INIT:"
- Multiple lots for same symbol → multiple INIT_BUY transactions (different dates)
```

### Validation
```bash
pytest tests/test_activity_parser.py tests/test_unrealized_parser.py -v
```

---

## Phase 3 — FIFO Engine & Holdings
**Goal:** Replay full transaction history to produce current open positions.

### Deliverables
- `portfolio/engine/fifo.py`
- `portfolio/engine/holdings.py`
- `portfolio/engine/cash.py`
- `tests/test_fifo.py`
- `tests/test_holdings.py`  (cutoff + skip-and-flag)
- `tests/test_cash.py`      (reconstruct + reconcile)

### `fifo.py`
```python
from collections import deque

def build_lots(
    transactions: list[Transaction],
) -> dict[tuple[str, str], deque[Lot]]:
    """
    Replay BUY, INIT_BUY, SELL transactions sorted by trade_date ascending.
    Key: (account_number, symbol)

    BUY / INIT_BUY:
        append Lot(account_number, symbol, trade_date, quantity, price) to deque

    SELL (quantity is negative):
        shares_to_sell = abs(quantity)
        while shares_to_sell > 0:
            lot = deque[0]
            if lot.quantity <= shares_to_sell:
                shares_to_sell -= lot.quantity
                deque.popleft()
            else:
                lot.quantity -= shares_to_sell
                shares_to_sell = 0

    Ignore: DIVIDEND, INTEREST, CASH_IN, CASH_OUT, ADR_FEE, TAX_WITHHOLDING, REINVEST

    Raise ValueError if SELL exceeds available lots (oversell).
    """

def compute_positions(
    lots_by_key: dict[tuple[str, str], deque[Lot]],
    account_registrations: dict[str, str],  # account_number → account_registration
) -> list[Position]:
    """
    Convert lot deques into Position objects. Exclude positions with quantity == 0.
    """
```

### `holdings.py`
```python
def filter_and_partition(
    transactions: list[Transaction],
    account_state: dict[str, dict],   # from account_state.json
) -> tuple[list[Transaction], list[str]]:
    """
    Apply the cutoff + skip-and-flag rules BEFORE lot building:
    - bootstrapped accounts: drop activity rows where trade_date <= init_date
      (INIT_BUY rows are exempt — they ARE the bootstrap). Keep activity after.
    - un-bootstrapped accounts (no entry in account_state): drop ALL their rows
      and return the account_number in the skipped list for the Run Log.
    Returns (kept_transactions, skipped_account_numbers).
    """

def compute_holdings(
    transactions: list[Transaction],
    account_state: dict[str, dict],
) -> tuple[list[Position], list[str]]:
    """
    Full pipeline:
    1. filter_and_partition(transactions, account_state)  → kept, skipped
    2. Sort kept by trade_date ascending (INIT_BUY before same-date BUY)
    3. build_lots(kept)
    4. compute_positions(lots_by_key, account_registrations)
    Returns (positions, skipped_accounts).
    """

def verify_against_snapshot(
    positions: list[Position],
    snapshot: dict[tuple[str, str], float],
    tolerance: float = 0.01,
) -> list[str]:
    """
    Compare computed vs. Merrill snapshot quantities.
    Returns list of strings: "OK" lines and "MISMATCH" lines.
    Tolerance handles floating-point rounding in fractional shares.
    """
```

### `cash.py` — reconstruct + reconcile (Decision 1)
```python
def reconstruct_cash(
    transactions: list[Transaction],
    bootstrap_cash: dict[str, float],   # {account: starting $} from Holdings snapshot
    account_state: dict[str, dict],
) -> dict[str, float]:
    """
    Running cash per account = bootstrap balance + replayed activity deltas
    (post-cutoff only). See the double-count hazard in Locked Decisions:

    DEFAULT (safe) model — drive cash ONLY from the cash-account rows
    themselves (990156937 sweep + IIAXX), because those rows already net the
    economic effect of trades/dividends/fees as they settle into the sweep:
        delta = -amount   for each cash-account row (Deposit (19.00) → +19;
                          Withdrawal 9,125.00 → -9,125; Bank Interest → +amount)
    Do NOT also add BUY/SELL/DIVIDEND amounts under this model — they would
    double-count against the sweep settlement rows.

    ⚠️ MUST be validated against a real multi-month activity set before trusting:
    confirm reconstructed balances converge to the next Holdings snapshot. If the
    sweep rows turn out NOT to capture every dividend, switch to the economic
    model (sum trade/dividend/fee amounts, treat sweep Deposit/Withdrawal as
    internal transfers). Phase 3 validation picks the correct model empirically.
    """

def reconcile_cash(
    reconstructed: dict[str, float],
    snapshot_cash: dict[str, float] | None,   # from Holdings CSV this run, if any
    as_of_date: date,
    tolerance: float = 0.01,
) -> list[CashBalance]:
    """
    Pair reconstructed vs snapshot per account; CashBalance.drift surfaces
    mismatches. Lines feed the Run Log cash_reconciliation column.
    """
```

### INIT_BUY sort order
When multiple INIT_BUY rows exist for the same (account, symbol) with different acquisition dates, they must sort by acquisition date so FIFO depletion works correctly. Use `trade_date` for sort key.

### Tests
```python
# test_fifo.py
- 3 buys + 1 partial sell → correct remaining lots and quantities
- FIFO order: oldest lot depleted first
- Fractional shares: 30.576 + 50.424 → sell 10.0 → correct remainder
- INIT_BUY treated identically to BUY for lot building
- Multiple INIT_BUY lots for same symbol sort by date before regular BUYs
- Identical INIT_BUY lots are BOTH kept (AXP-style duplicate lots)
- Oversell raises ValueError with descriptive message
- DIVIDEND rows are ignored (no lot change)

# test_holdings.py
- Activity with trade_date <= account init_date is dropped (cutoff)
- Activity with trade_date > init_date is kept
- Un-bootstrapped account: all its rows dropped, account in skipped list
- Bootstrapped + un-bootstrapped accounts in same run → former processes, latter skipped
- verify_against_snapshot flags a known MISMATCH and known OK lines

# test_cash.py
- Bootstrap from Holdings cash map; sweep Deposit/Withdrawal/Interest deltas apply
- Deposit "(19.00)" → +19 ; Withdrawal "9,125.00" → -9,125
- reconcile_cash: drift is None when no snapshot; computed when snapshot present
- Roth account uses IIAXX as its cash account, CMA uses 990156937
```

### Validation
```bash
pytest tests/test_fifo.py -v
# Then run against real sample data:
python -c "
from portfolio.parsers.activity_parser import parse_activity_csv
from portfolio.parsers.unrealized_parser import parse_unrealized_csv
from portfolio.engine.holdings import compute_holdings, verify_against_snapshot
from portfolio.parsers.holdings_parser import parse_holdings_csv

txns = parse_unrealized_csv('tests/sample_data/unrealized_sample.csv')
txns += parse_activity_csv('tests/sample_data/activity_sample.csv')
positions = compute_holdings(txns)
snapshot = parse_holdings_csv('tests/sample_data/holdings_sample.csv')
diffs = verify_against_snapshot(positions, snapshot)
for d in diffs: print(d)
"
```

---

## Phase 4 — Market Data (yfinance + Cache)
**Goal:** Fetch fundamental data for all held symbols; cache to Drive; provide 4 years of ROE history.

### Deliverables
- `portfolio/market/symbol_overrides.py`
- `portfolio/market/yfinance_client.py`
- `portfolio/metrics/fundamentals.py`

### `yfinance_client.py`
```python
@dataclass
class StockFundamentals:
    symbol: str               # normalized (yfinance) symbol
    pe_ratio: float | None
    dividend_yield: float | None
    roe_current: float | None # info["returnOnEquity"] (trailing 12mo)
    roe_1y: float | None      # prior year (from yfinance financials if available)
    roe_2y: float | None
    roe_3y: float | None
    roe_4y: float | None
    net_income: float | None
    book_value: float | None
    fetched_at: str           # ISO datetime string

def fetch_fundamentals(
    symbols: list[str],
    cache: dict,
    ttl_hours: int = 24,
) -> dict[str, StockFundamentals]:
    """
    For each symbol:
    - Check cache; if hit and not stale (< ttl_hours old), use cache
    - Otherwise: yf.Ticker(symbol).info → extract fields
    - Historical ROE: yf.Ticker(symbol).financials for net_income per year;
      yf.Ticker(symbol).balance_sheet for book_value per year.
      Compute roe_Ny = net_income_Ny / book_value_Ny for up to 4 prior years.
    - On failure: warn, use stale cache if available, else return None fields
    Update cache dict in-place. Caller is responsible for saving cache to Drive.
    """
```

### Cache format (`yfinance_cache.json`)
```json
{
  "AAPL": {
    "pe_ratio": 28.4,
    "dividend_yield": 0.0055,
    "roe_current": 1.47,
    "roe_1y": 1.60,
    "roe_2y": 1.55,
    "roe_3y": 1.45,
    "roe_4y": 1.30,
    "net_income": 94000000000,
    "book_value": 64000000000,
    "fetched_at": "2026-01-30T10:00:00"
  }
}
```

### Notes on yfinance ROE history
- `ticker.financials` returns a DataFrame with annual columns (dates as column headers)
- Row `"Net Income"` provides net income per fiscal year
- `ticker.balance_sheet` row `"Stockholders Equity"` provides book value per year
- Take the 4 most recent annual columns beyond the current year for roe_1y–roe_4y
- yfinance free tier provides up to 4 years of annual financials — sufficient

### Validation
```bash
python -c "
import json
from portfolio.market.yfinance_client import fetch_fundamentals
cache = {}
results = fetch_fundamentals(['AAPL', 'BRK-B', 'NVDA', 'IX', 'TM'], cache, ttl_hours=24)
for sym, f in results.items():
    print(sym, f.roe_current, f.roe_1y, f.pe_ratio)
print('cache keys:', list(cache.keys()))
"
```

---

## Phase 5 — Google Sheets Output
**Goal:** Write all output tabs to a Google Sheet using gspread + Google OAuth.

### Deliverables
- `portfolio/sheets/writer.py`
- Manual: create Google Sheet → copy ID into config

### Auth
```python
def get_gspread_client(credentials=None) -> gspread.Client:
    """
    If credentials provided (Colab flow): use gspread.authorize(credentials)
    If running locally: use InstalledAppFlow with credentials.json
    """
```

### Tab initialization
On first run (tabs don't exist yet), create each tab and write the header row.

### Writer functions
```python
def write_transactions(
    sh: gspread.Spreadsheet,
    transactions: list[Transaction],
    existing_keys: set[tuple],   # dedup keys already in sheet
) -> int:
    """Append only rows whose dedup_key is not in existing_keys. Return count added."""

def load_existing_transaction_keys(sh: gspread.Spreadsheet) -> set[tuple]:
    """Read Transactions tab, return set of dedup_key tuples."""

def write_holdings(
    sh: gspread.Spreadsheet,
    positions: list[Position],
) -> None:
    """
    Overwrite Holdings tab (clear then rewrite).
    Columns: as_of_date | account_number | account_registration | symbol | quantity | avg_cost | cost_basis
    Note: current_price and market_value columns use GOOGLEFINANCE formulas —
    write them as formulas, e.g.: =IFERROR(GOOGLEFINANCE(D2,"price"),0)
    """

def write_cash(
    sh: gspread.Spreadsheet,
    balances: list[CashBalance],
) -> None:
    """
    Overwrite Cash tab (clear then rewrite).
    Columns: as_of_date | account_number | account_registration | owner |
             cash_account | reconstructed | snapshot | drift
    Owner from config.ACCOUNT_OWNERS (blank if unmapped). Non-zero drift should
    stand out (the runner also logs it to Run Log cash_reconciliation).
    """

def write_stock_metrics(
    sh: gspread.Spreadsheet,
    fundamentals: dict[str, StockFundamentals],
    run_date: date,
) -> None:
    """
    Overwrite Stock Metrics tab (clear then rewrite).
    Python-written columns: as_of_date | symbol | pe_ratio | dividend_yield |
        roe_current | roe_1y | roe_2y | roe_3y | roe_4y | net_income | book_value
    Adjacent GOOGLEFINANCE formula columns (written as formulas by this function):
        current_price: =IFERROR(GOOGLEFINANCE(B2,"price"),"N/A")
        high_52wk:     =IFERROR(GOOGLEFINANCE(B2,"high52"),"N/A")
        low_52wk:      =IFERROR(GOOGLEFINANCE(B2,"low52"),"N/A")
    """

def write_run_log(sh: gspread.Spreadsheet, entry: RunLogEntry) -> None:
    """Append one row to Run Log tab."""
```

### GOOGLEFINANCE formula strategy
Python writes `=IFERROR(GOOGLEFINANCE(B2,"price"),"N/A")` as a string value into cells. Google Sheets treats strings starting with `=` as formulas. This keeps price data always-fresh without Python making any price API calls.

### Validation
- Run writer against a test Sheet
- Verify tabs created with correct headers
- Run twice → no duplicate Transactions rows
- Verify GOOGLEFINANCE formulas render as prices (not formula text)

---

## Phase 6 — Google Drive Integration
**Goal:** Read CSVs from upload folder; archive after processing; cache yfinance data on Drive.

### Deliverables
- `portfolio/drive/archiver.py`
- Manual: create subfolders in Drive root → set their IDs as env vars (see config.py)

### Drive folder setup (folders created; real IDs supplied via env, kept in .secrets.local.md)
| Folder | Env var |
|--------|---------|
| upload | `DRIVE_UPLOAD_FOLDER_ID` |
| processed | `DRIVE_PROCESSED_FOLDER_ID` |
| failed | `DRIVE_FAILED_FOLDER_ID` |
| cache | `DRIVE_CACHE_FOLDER_ID` |

### `archiver.py`
```python
def build_drive_service(credentials):
    """Build Google Drive API v3 service from credentials."""

def list_pending_csvs(service, upload_folder_id: str) -> list[dict]:
    """List all .csv files in upload folder. Returns list of {id, name} dicts."""

def download_csv(service, file_id: str, dest_path: Path) -> None:
    """Download Drive file to local temp path."""

def move_to_processed(service, file_id: str, filename: str,
                      upload_folder_id: str, processed_folder_id: str) -> None:
    """Add file to processed/ folder, remove from upload/ folder."""

def move_to_failed(service, file_id: str, filename: str,
                   upload_folder_id: str, failed_folder_id: str) -> None:
    """Add file to failed/ folder, remove from upload/ folder."""

def load_yfinance_cache(service, cache_folder_id: str) -> dict:
    """Download yfinance_cache.json from Drive cache folder. Return {} if not found."""

def save_yfinance_cache(service, cache_folder_id: str, cache: dict) -> None:
    """Upload/overwrite yfinance_cache.json in Drive cache folder."""
```

### Validation
- Drop a test CSV into the upload folder via Drive UI
- Run archiver in isolation, verify file moves to processed/
- Verify cache round-trip (write dict → download → same dict)

---

## Phase 7 — Colab Notebook
**Goal:** A single notebook anyone can open in Colab and run end-to-end.

### Deliverables
- `notebook/portfolio_update.ipynb`

### `runner.py`
```python
def run_update(credentials=None) -> None:
    """
    Full orchestration:
    1.  Build Drive service from credentials
    2.  Load account_state.json from Drive cache (init dates + bootstrap files)
    3.  List CSVs in upload folder; download each to /tmp, detect type
    4.  Separate: unrealized_files, holdings_files, activity_files, other_files
    5.  Parse unrealized files → INIT_BUY transactions; for any NEW account,
        record its COB Date as init_date in account_state (first intake wins)
    6.  Parse holdings files → equity verification map + cash bootstrap map
    7.  Parse activity files → activity transactions (settled only; strip Type fields)
    8.  Connect to Sheets; load existing transaction dedup keys
    9.  Filter out already-seen INIT_BUY (by source_file) and activity transactions;
        flag identical-fill activity collisions for human review
    10. Write new transactions to Transactions tab
    11. compute_holdings(all_txns, account_state) → positions + skipped_accounts
        (applies per-account cutoff; skips un-bootstrapped accounts)
    12. verify_against_snapshot(positions, holdings equity map) → diffs
    13. Write Holdings tab
    14. reconstruct_cash + reconcile_cash against holdings cash map → CashBalances
    15. Write Cash tab
    16. Collect all held symbols (from positions)
    17. Load yfinance cache from Drive; fetch fundamentals (respects TTL); save cache
    18. Write Stock Metrics tab
    19. Save updated account_state.json to Drive
    20. Move successfully processed CSVs to processed/; failed to failed/
    21. Write Run Log entry (incl. accounts_skipped, cash_reconciliation)
    22. Print summary
    """
```

### Notebook cells
```
Cell 1 — Install
!pip install -q git+https://github.com/humble-methods/investment-sheet-manager.git@main

Cell 2 — Auth
from google.colab import auth
auth.authenticate_user()
import google.auth
credentials, _ = google.auth.default()

Cell 3 — Run
from portfolio.runner import run_update
run_update(credentials=credentials)
```

### Validation
- Run notebook in Colab from scratch (no cached state)
- Drop one Unrealized CSV + one Activity CSV into upload folder first
- Verify: Transactions tab populated, Holdings tab populated, Stock Metrics tab populated, Run Log entry written, CSVs moved to processed/

---

## Phase 8 — Hardening & Cleanup
**Goal:** Make the system robust for ongoing use.

### Items
- Error isolation: if one CSV fails to parse, log to Run Log and move to failed/, continue with others
- Unknown `(Type, Description 1)` combinations: log warning and classify as `UNKNOWN`, don't crash
- Summary printout at end of notebook run (rows added, symbols fetched, errors)
- README.md: setup guide (Drive folder setup, credentials.json, initial Unrealized CSV bootstrap)
- `.gitignore`: exclude `credentials.json`, `token.json`, `*.csv`, `__pycache__`, `.env`

---

## Phase Sequence & Dependencies

```
Phase 1 (Foundation)
    └── Phase 2 (Parsing)
            ├── Phase 3 (FIFO Engine)
            │       └── Phase 5 (Sheets Output)
            │               └── Phase 6 (Drive)
            │                       └── Phase 7 (Notebook) ← integrates all
            └── Phase 4 (Market Data)  ← can start in parallel with Phase 3
                    └── feeds into Phase 5 (Stock Metrics tab)
                    └── Phase 8 (Hardening)
```

---

## Definition of "Done" (MVP)

1. Hank or Mom drops Merrill CSV(s) into the shared Drive upload folder
2. Opens the Colab notebook and clicks Run All
3. Google Sheet is updated: new Transactions rows, current Holdings, Stock Metrics with GOOGLEFINANCE prices, Run Log entry
4. CSV moved to processed/ folder
5. No manual steps beyond (1) downloading the CSV from Merrill and (2) clicking Run All in Colab
