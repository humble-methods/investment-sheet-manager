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

### Corporate actions: partially handled (amended post-MVP)
Originally **all** corporate actions were out of scope (manual intervention; flag unmapped
combos as `UNKNOWN`). Two amendments:
- **Ticker renames are now handled in code** via `config.TICKER_RENAMES` (Phase 18) — a rename
  otherwise oversells (bootstrap lots under the old ticker, SELL under the new). First case `ATGE → CVSA`.
- **Splits / stock dividends** (share-changing, $0 amount) are handled in code (Phase 18) via a new
  `SPLIT` type + in-place lot scaling (`qty ×ratio`, `unit_cost ÷ratio`, basis + acq date unchanged),
  applied during FIFO replay. VGT ×8 confirmed against the 06/26/2026 tax-lots file. **Built in Phase 19.**
- **Inbound cash transfers** (`Funds Received` wires → `CASH_IN`) now credit reconstructed cash;
  **Roth contributions** (`Current Year Contribution` → `CONTRIBUTION_INFO`) are recorded-only and
  excluded from cash math (the IIAXX deposit already books them). **Built in Phase 20.** Mergers stay manual.

### Price strategy: current via GOOGLEFINANCE, history via Python (Phase 11, partial reversal)
The original rule ("all price data via GOOGLEFINANCE; Python never fetches prices") is **partially reversed**: **5-year weekly historical closes are fetched by Python/yfinance** and written to a dedicated `Price History` tab, while the **current price stays a live GOOGLEFINANCE formula** on Holdings and Stock Metrics. Rationale: a date-ranged series is a 2-D spill that can't live one-row-per-symbol, and the user wants the raw closes available as values. The 52-week high/low GOOGLEFINANCE columns are removed in favor of the full history. (Amends the Phase 4/5 notes below.)

### Metrics scope: all recorded symbols + watchlist (Phase 10)
Stock Metrics and Price History cover **every symbol ever recorded in the Transactions tab (held OR since sold)** unioned with a human-editable **`Watchlist` tab** (single `Symbol` column), minus cash. This broadens the original "held symbols only" scope.

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

> **Superseded by Phase 17:** the flat `roe_1y…roe_4y` dataclass fields + cache keys below are
> replaced by a calendar-year-keyed `roe_by_year` dict that accumulates across runs (10-yr sheet
> capacity). See Phase 17 for the current shape.

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
> **Note:** The column orders and snake_case header labels shown below are the original Phase-5 layout. **Phases 9–11 supersede them** — Title Case headers, `as_of_date` moved last, 52-week high/low removed, and a new `Price History` tab. See those phases for the current schema.

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
Python writes `=IFERROR(GOOGLEFINANCE(B2,"price"),"N/A")` as a string value into cells. Google Sheets treats strings starting with `=` as formulas. This keeps the **current** price always-fresh without Python making live-quote API calls.

> **Partially superseded by Phase 11:** the current price stays a GOOGLEFINANCE formula, but the 52-week high/low formulas are removed and **5-year weekly historical closes are now fetched by Python/yfinance** into a `Price History` tab. After Phase 9 the symbol cell refs also shift (Holdings symbol→C, Stock Metrics symbol→A).

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

## Phase 9 — Human-Readable Headers + `as_of_date` Last (dedup-safe)
**Goal:** Title Case labels on every output tab and `as_of_date` as the LAST column, without breaking the Transactions dedup that reads rows back from the sheet.

### Deliverables
- `portfolio/sheets/writer.py` (updated)
- `tests/test_writer.py` (updated)

### The migration trap (why this is sequenced first)
The Transactions tab is append-only and deduped by reading existing rows back. `_read_tab_rows` keyed each row dict off the sheet's **header text**, and `_row_canonical_key` looks up `row["trade_date"]`, `row["tx_type"]`, etc. Relabeling the header on an **already-deployed** sheet (whose header row is still snake_case when read, before any rewrite) would fail every dedup match and **mass-reimport duplicate transactions**. Only the Transactions tab is read back; Holdings/Cash/Stock Metrics/Run Log are write-only, so reordering their columns is safe.

### Changes
- Display labels (`*_HEADERS`) are now **Title Case in the new column order**; a parallel `TRANSACTIONS_KEYS` holds the stable snake_case field names in the **unchanged** Transactions order.
- `_read_tab_rows(ws, keys=None)` keys rows by **position** against `keys` when given (the sheet's header row is skipped but ignored); the Transactions readers pass `TRANSACTIONS_KEYS`. `_row_canonical_key` is unchanged.
- `write_holdings` / `write_cash` / `write_stock_metrics` reorder with `as_of_date` last and fix the GOOGLEFINANCE cell refs (Holdings: symbol→C ⇒ `GOOGLEFINANCE(C{r},"price")`, market value `D{r}*G{r}`; Stock Metrics: symbol→A).
- `_refresh_header(ws, headers)` rewrites row 1 on the append-only tabs (Transactions, Run Log) so their visible header switches to Title Case — cosmetic only; correctness is position-based. Keyword args (`values=`, `range_name=`) for gspread v5/v6 compatibility.

### Final tab headers (Title Case)
- **Transactions** (order unchanged): `Trade Date | Settlement Date | Status | Account Number | Account Registration | Transaction Type | Description | Symbol | Quantity | Price | Amount | Source File`
- **Holdings**: `Account Number | Account Registration | Symbol | Quantity | Average Cost | Cost Basis | Current Price | Market Value | As Of Date`
- **Cash**: `Account Number | Account Registration | Owner | Cash Account | Reconstructed | Snapshot | Drift | As Of Date`
- **Stock Metrics**: `Symbol | P/E Ratio | Dividend Yield | ROE (Current) | ROE (1Y Ago) | ROE (2Y Ago) | ROE (3Y Ago) | ROE (4Y Ago) | Net Income | Book Value | Current Price | As Of Date`
- **Run Log** (order unchanged; `Run Timestamp` is the log key, not an as_of_date): `Run Timestamp | Files Processed | Init Rows Added | Transactions Added | Accounts Skipped | Errors | Holdings Changed | Cash Reconciliation | Duration (Sec) | Notes`

### Tests
- `test_dedup_readback_is_position_based`: the OLD snake_case header AND the NEW Title Case header both yield identical dedup keys (the regression guard).
- Updated formula/column-index assertions across Holdings/Cash/Stock Metrics.

### Validation
```bash
python3 -m pytest tests/test_writer.py -q   # use python3, not the 3.9 .venv
```

---

## Phase 10 — Broaden Metrics Scope + Watchlist Intake
**Goal:** Stock Metrics covers **every recorded symbol (held OR since sold)** plus a human-editable watchlist — not just currently-held positions.

### Deliverables
- `portfolio/sheets/writer.py` (`load_recorded_symbols`, `load_watchlist_symbols`, `TAB_WATCHLIST`, `WATCHLIST_HEADERS`)
- `portfolio/runner.py` (symbol-collection rewrite)
- `tests/test_writer.py` (updated)

### Changes
- `load_recorded_symbols(sh)` → distinct non-blank `symbol` values from the Transactions tab (read position-based via `TRANSACTIONS_KEYS`); `[]` if the tab is missing. Raw symbols — caller normalizes.
- `load_watchlist_symbols(sh)` → `_ensure_tab(sh, "Watchlist", ["Symbol"])` (creates the tab with a `Symbol` header if missing, never clobbers user rows), then reads the `Symbol` column (case-insensitive header match; default col 0).
- `runner.py` step 16 (run **after** transactions are written so this run's new symbols count):
  ```python
  recorded  = load_recorded_symbols(sh)
  watchlist = load_watchlist_symbols(sh)
  symbols   = normalize_all([*recorded, *watchlist])   # dedups, drops cash/blank, BRKB→BRK-B
  ```
  `recorded ⊇ held` (no holding without a recorded BUY/INIT_BUY), so coverage strictly broadens. The same `symbols` list feeds both `fetch_fundamentals` and `fetch_price_history`.

### Tests
- `test_load_recorded_symbols_distinct_nonblank`, `test_load_recorded_symbols_missing_tab`.
- `test_load_watchlist_symbols_reads_symbol_column`, `test_load_watchlist_symbols_missing_tab_creates_and_returns_empty`.

### Validation
```bash
python3 -m pytest tests/test_writer.py -q
```

---

## Phase 11 — 5-Year Weekly Closes via yfinance → `Price History` tab
**Goal:** Replace the `high_52wk` / `low_52wk` GOOGLEFINANCE columns with **5 years of weekly closing prices fetched in Python** (yfinance); keep `current_price` as a live GOOGLEFINANCE formula. See the **Price strategy** Locked Decision above.

### Deliverables
- `portfolio/market/yfinance_client.py` (`PriceHistory`, `fetch_price_history`, `_fetch_history_one`)
- `portfolio/drive/archiver.py` (`load_price_history_cache`, `save_price_history_cache`)
- `portfolio/sheets/writer.py` (`write_price_history`, `TAB_PRICE_HISTORY`; 52wk columns removed)
- `portfolio/runner.py` (fetch + write wiring)
- `tests/test_yfinance_client.py`, `tests/test_writer.py` (updated)

### Changes
- `fetch_price_history(symbols, cache, ttl_hours=24, period="5y", interval="1wk") -> {symbol: PriceHistory}` mirrors `fetch_fundamentals`' cache-first / stale-on-failure flow and reuses the monkeypatchable `_ticker`. `_fetch_history_one` → `ticker.history(period, interval, auto_adjust=False)`, takes `Close`, converts the DatetimeIndex to ISO dates, drops NaNs.
- Second Drive cache file `price_history_cache.json` (`_CACHE_PRICE_HISTORY`) with `load_/save_price_history_cache` wrappers over `_load_json`/`_save_json`.
- `write_price_history(sh, histories)` clears the `Price History` tab and builds a **unified, sorted Date axis** (the union of every symbol's dates) so symbols with different history lengths align; blanks fill the gaps. `Date` is column A; symbols are the remaining columns, sorted alphabetically.
- `STOCK_METRICS_HEADERS` and `write_stock_metrics` drop `52-Week High` / `52-Week Low` (`current_price` stays).

### Cache format (`price_history_cache.json`)
```json
{ "AAPL": { "symbol": "AAPL", "dates": ["2021-06-14", "..."], "closes": [129.6, "..."], "fetched_at": "2026-06-15T10:00:00" } }
```

### Tests
- `test_yfinance_client.py`: `FakeTicker` gains a `.history()` method; cache-miss / NaN-skip / fresh-skip / stale-refetch / failure-keeps-stale / failure-no-cache / normalize-dedup for `fetch_price_history`.
- `test_writer.py`: `test_write_price_history_unified_date_axis` (overlapping + disjoint dates align with blanks), `test_write_price_history_empty`, `test_write_stock_metrics_no_52wk_columns`.

### Validation
```bash
python3 -m pytest -q   # full suite green
```

---

## Phase 12 — Composition Tab (REMOVED)
**Status:** Removed after the first build. Market-value vs cost-basis weighting is better done with the
sheet's own pivot/chart tooling than a Python-maintained data tab. Deleted `portfolio/metrics/composition.py`,
`write_composition` + `TAB_COMPOSITION`/`COMPOSITION_HEADERS`, `COMPOSITION_OTHER_THRESHOLD`,
`tests/test_composition.py`, and runner step 18b. May return later as a sheet-native view.

---

## Phase 13 — Performance Tab (lifetime XIRR + per-year Modified Dietz)
**Goal:** Annualized money-weighted return per symbol (lifetime) + per-calendar-year returns, each total (dividends in) and price (out), comparable across symbols and entry years.

### Deliverables
- `portfolio/metrics/performance.py` (`xirr`, `modified_dietz`, `lifetime_cashflows`, `year_returns`, `build_performance`, `SymbolPerformance`, `YearPerformance`)
- `portfolio/engine/holdings.py` (`positions_as_of` — reuses `filter_and_partition` + `build_lots`)
- `portfolio/sheets/writer.py` (`write_performance`, `write_performance_by_year` + tabs/headers)
- `portfolio/runner.py` (step 18c), `tests/test_performance.py`, `tests/test_holdings.py`, `tests/test_writer.py`

### Approach
- **Lifetime = annualized XIRR** (Newton + bisection fallback, no scipy): buys/sells at `tx.amount`, dividends (total only), terminal = current value. **Per-year = non-annualized Modified Dietz** (avoids partial-year annualization blow-up). `Income = total − price`.
- Per symbol consolidated across accounts + a pooled `PORTFOLIO` row (invested-sleeve XIRR; not whole-account — sidesteps Decision 19). Total leg nets ADR fees + foreign withholding.
- Per-year begin/end values = year-boundary shares (`positions_as_of(Dec 31)`) × year-end weekly close from `PriceHistory`; reuses the **same filtered txn set** as Holdings (no double counting).
- **Presentation:** these two tabs are **backing data** only; the interactive side-by-side view is the new `Performance Compare` tab (Phase 16). Computation here is unchanged (calendar-year Modified Dietz), fed by the 5-yr weekly series now retained in the Drive cache (Phase 15).

### Tests
`xirr` known IRRs + sign edge cases → None; `modified_dietz` closed-form + time-weighting; `lifetime_cashflows` total vs price (fee netting); `year_returns` extraction; `positions_as_of` mid-year buy/sell at a year boundary; `build_performance` integration (chained year values, PORTFOLIO row).

### Validation
```bash
python3 -m pytest tests/test_performance.py tests/test_holdings.py -q   # yfinance monkeypatched, never live
```

### Caveats (in tab notes / docs)
Per-year coverage is bounded by the 5-yr price-history window; pre-cutoff dividends are absent so total return understates income for long-held bootstrapped lots; terminal/current price is the latest weekly close (≤ ~1 wk stale).

---

## Phase 14 — Opportunity Cost of Idle Cash (REMOVED)
**Status:** Removed after the first build — the idle-cash-drag framing wasn't as exhaustive/useful as hoped
and needs rethinking. Deleted `portfolio/metrics/opportunity.py`, `engine/cash.py::cash_balance_series`,
`write_opportunity_cost` + tab/headers, `tests/test_opportunity.py` (+ its `test_cash.py` cases), and runner
step 18d. `reconstruct_cash` and the Cash tab are untouched. To be re-evaluated.

---

## Phase 15 — Price History → anniversary snapshots, symbols as rows
**Goal:** Replace the wide weekly Price History tab (one column per symbol, ~260 date rows) with a compact
**one-row-per-symbol** view of anniversary closes (Today, 1Y–5Y Ago). The full weekly series stays in the
Drive cache for the Performance year-boundary math.

### Deliverables
- `portfolio/metrics/pricing.py` (NEW; `close_on_or_before`, `anniversary`) — shared, dependency-light (stdlib only).
- `portfolio/metrics/performance.py` (`_close_on_or_before` now aliases the shared helper).
- `portfolio/sheets/writer.py` (`write_price_history` reshaped; `PRICE_HISTORY_HEADERS`).
- `tests/test_pricing.py` (NEW), `tests/test_writer.py`.

### Approach
- yfinance fetch + `price_history_cache.json` **unchanged** (still 5-yr weekly) — only the *tab* changes.
- Per symbol (sorted): write `Symbol | Today | 1Y Ago | … | 5Y Ago`, each = `close_on_or_before(history, anniversary(today, k))`. Blank where the symbol lacks history that far back. `anniversary` = `today.replace(year=today.year − k)` with a Feb 29 → Feb 28 fallback.
- `write_price_history(sh, histories, today=None)` gains `today` for deterministic tests.

### Tests
`close_on_or_before` last-on-or-before + None/empty; `anniversary` basic + leap-day; `write_price_history` one row per symbol, correct anniversary picks, blanks for short history, header-only when empty.

### Validation
```bash
python3 -m pytest tests/test_pricing.py tests/test_writer.py -q
```

---

## Phase 16 — Performance Compare (interactive side-by-side cards)
**Goal:** Replace "all symbols listed at once" with a **sheet-driven** comparison: pick a few symbols and see
their lifetime + per-year metrics as side-by-side cards. Computation stays in Python; interaction lives in the sheet.

### Deliverables
- `portfolio/sheets/writer.py` (`write_performance_compare`, `_performance_compare_grid`, `_compare_lifetime_formula`, `_compare_year_formula`, `_apply_compare_dropdowns`, `TAB_PERFORMANCE_COMPARE`, `PERFORMANCE_COMPARE_SLOTS = 5`, `PERFORMANCE_COMPARE_YEARS = 6`).
- `portfolio/runner.py` (step 18b, after the Performance data tabs).
- `tests/test_writer.py`.

### Approach
- **Backing data** = the existing `Performance` (lifetime) + `Performance By Year` (long) tabs, rewritten each run.
- **Presentation** = a scaffolded tab: col A metric labels; row-1 cols B… are fixed **single-select dropdown slots** sourced from `=Performance!$A$2:$A`. Lifetime rows `VLOOKUP` the Performance tab; per-year rows `INDEX/MATCH` Performance By Year on a `symbol|year` key built inline with `ARRAYFORMULA`. Year rows are `YEAR(TODAY())`-relative so they self-update.
- **Set-up-once:** `write_performance_compare` no-ops if the tab exists (never clobbers the user's picks); cards refresh live because they're formulas. Dropdowns + frozen header/label applied via `sh.batch_update` (`setDataValidation` `ONE_OF_RANGE`).
- **Decisions:** calendar-year (not trailing) per-period returns; fixed dropdown slots (not multi-select) — the Sheets API can't create true multi-select "chip" dropdowns (UI-only).

### Tests
Scaffolds when absent (grid: label col, blank slots, VLOOKUP lifetime rows, `YEAR(TODAY())` year rows); sets `ONE_OF_RANGE` validation over the N slots; **idempotent** no-op when the tab already exists.

### Validation
```bash
python3 -m pytest tests/test_writer.py -q
```

---

## Phase 17 — ROE History by Calendar Year (accumulating) + 10-Year Capacity
**Goal:** Make Stock Metrics ROE accumulate toward 10 years even though yfinance returns only ~4
annual columns per fetch, and surface symbols that come back empty from yfinance. **Status: built.**

### Deliverables
- `portfolio/metrics/fundamentals.py` (`roe_history` returns `{calendar_year: roe}`; pairs income/equity by year; `_annual_pairs`, `_column_year`)
- `portfolio/market/yfinance_client.py` (`StockFundamentals.roe_by_year` replaces `roe_1y…roe_4y`; `fetch_fundamentals` merges fresh years into the cached dict; `is_empty_fundamentals`)
- `portfolio/sheets/writer.py` (`STOCK_METRICS_HEADERS` → 10 relative `ROE (NY Ago)` cols; `write_stock_metrics` maps calendar-year cache → relative columns by `run_year − N`)
- `portfolio/config.py` (`EXPECTED_MISSING_SYMBOLS`), `portfolio/runner.py` (Run Log notes for blank fundamentals/history)
- `tests/test_fundamentals.py`, `tests/test_yfinance_client.py`, `tests/test_writer.py`

### Approach
- **Accumulate by calendar year:** the cache keys ROE by the fiscal period-end's calendar year (string keys); each run unions newly-fetched years into the prior cached dict (fresh wins). Old years persist after they age out of yfinance's ~4-yr window, so the cache grows toward 10 over time. Never replace `roe_by_year` wholesale.
- **Relative columns** (`ROE (1Y Ago)…(10Y Ago)`): the writer projects the calendar-year cache onto column `k` where `year == run_year − k`. ~4 fill now; later columns self-fill on future runs.
- **Surface blanks:** `is_empty_fundamentals` → runner logs `No yfinance fundamentals: …` / `No price history: …` to the Run Log, split into expected (`EXPECTED_MISSING_SYMBOLS`, e.g. `SFGYY`) vs. flagged.

### Tests
`roe_history` keyed by year + income/equity aligned by year (not position); cross-run accumulation retains old years; `is_empty_fundamentals`; writer relative-year column mapping + 10 provisioned columns.

### Validation
```bash
python3 -m pytest tests/test_fundamentals.py tests/test_yfinance_client.py tests/test_writer.py -q
```

---

## Phase 18 — Ticker Renames (corporate-action symbol unification)
**Goal:** A security that changed tickers must unify its old (bootstrap) and new (activity) symbols so
the post-rename SELL doesn't oversell. **Status: built.**

### Deliverables
- `portfolio/models.py` (`CorporateAction` dataclass — structured rename record)
- `portfolio/config.py` (`TICKER_RENAMES: dict[str, CorporateAction]`, seeded `ATGE → CVSA`)
- `portfolio/market/symbol_overrides.py` (`normalize_symbol` applies `TICKER_RENAMES` BEFORE `SYMBOL_OVERRIDES`)
- `tests/test_symbol_overrides.py`, `tests/test_fifo.py`

### Approach
- Corporate-action normalization maps to a **structured `CorporateAction`** (`new_symbol`, `kind`, `note`), not a bare old→new string — extensible to splits/mergers (ratios, effective dates) in Phase 19 without reshaping call sites.
- `normalize_symbol` is the single chokepoint both parsers use via `clean_symbol`, so a rename there unifies bootstrap `INIT_BUY` lots and later activity at ingest. `normalize_all` then collapses any stale OLD ticker still on the Transactions tab, so yfinance fetches only the live ticker.
- Two-stage: rename (old→current, via `action.new_symbol`) first, then Merrill→Yahoo spelling override — a renamed ticker can still get a spelling fix.

### Tests
`normalize_symbol("ATGE") == "CVSA"`; rename-then-spelling chaining; `normalize_all` collapses old+new to one; FIFO regression — INIT_BUY under old ticker + full SELL under new ticker depletes cleanly (reproduces the reported `Oversell` crash).

### Validation
```bash
python3 -m pytest tests/test_symbol_overrides.py tests/test_fifo.py -q
```

---

## Phase 19 — Stock Splits / Stock Dividends (in-place lot scaling) (BUILT)
**Goal:** Handle the `$0.00`-amount share-change events the parser previously dropped as `UNKNOWN`
(Non-Obvious #26) by **adjusting the original lots in place** — never adding `$0`-cost shares at the
event date. **Status: built.** New `SPLIT` type + FIFO in-place scaling; `parse_holding_base` cross-check
logs a warning on divergence. Real-data VGT ×8 confirmed via unit fixture; KLAC ×10 still deferred.

### Decision (locked with user)
A split / stock dividend **scales the existing lots**: `quantity × ratio`, `unit_cost ÷ ratio`, with
**acquisition date and total cost basis unchanged**. Adding new `$0`-cost lots at the event date would
inject a phantom present-day acquisition that **wrecks the XIRR / annualized-return** metric — explicitly
rejected.

### Scope (04–06/2026 file)
- `SecurityTransactions / Stock Dividend Due Bill` (a `+N` due bill then a `−N` reversal) and
  `SecurityTransactions / Dividend` with a **share** quantity + `$0.00` amount (the `+N` delivery). Per
  `(account, symbol, event)` these **net to `+N`**. Examples: **VGT** `53X-69S37` 10→80 (**×8**);
  **KLAC** `73S-17D17` 34→340 (**×10**).

### Approach
- **New canonical type `SPLIT`** in the parser: map both `Stock Dividend Due Bill` and the `$0`/share
  `SecurityTransactions/Dividend` combos to `SPLIT`, carrying the **signed share quantity**. Disambiguate
  from a normal cash dividend on `amount == 0 and quantity is not None` (a real dividend has a nonzero
  amount and no quantity → stays `DIVIDEND`).
- **FIFO replay (`build_lots`)**: per `(account, symbol)` the event's **net delta = Σ SPLIT quantities**
  (so `+N / −N / +N → +N`; order- and cross-file-robust because replay sees every row). Ratio =
  `(running_qty + net_delta) / running_qty`; scale every open lot of that key (`qty *= ratio`,
  `unit_cost /= ratio`). Cross-check against the broker `HOLDING N` base parsed from Description 2 and
  **flag** a mismatch (signals our running qty diverged — e.g. a missed earlier event). Sort `SPLIT`
  after same-day BUY/INIT_BUY so the ratio sees the full pre-split position.

### Deliverables
- `portfolio/parsers/activity_parser.py` (TX_TYPE_MAP + `SPLIT` disambiguation; parse `HOLDING N` base)
- `portfolio/engine/fifo.py` (`SPLIT` lot-scaling during replay)
- `tests/test_activity_parser.py`, `tests/test_fifo.py`

### Validation
- Unit: `+N / −N / +N` resolves to one ×ratio scaling; lots scale qty **up** / cost **down**;
  **total cost basis and acquisition dates unchanged**; XIRR unaffected (no cashflow injected).
- Real data — **VGT confirmed** against `UnrealizedGainLossTaxLots_06262026` (`53X-69S37`): original lot
  10 sh @ $671.03 (basis $6,710.30, acq 7/7/2025) → **80 sh @ $83.88, basis $6,710.30, acq 7/7/2025**
  (×8: qty up, unit cost down, basis + acquisition date unchanged). Use this file as the end-to-end
  regression fixture. KLAC `73S-17D17` → 340 sh (×10) — **still deferred** (that account isn't in the file).
```bash
python3 -m pytest tests/test_activity_parser.py tests/test_fifo.py -q
```

### Open question (non-blocking)
Ratio source of truth — broker `HOLDING N` base (authoritative) vs. engine running qty. Plan uses running
qty with a `HOLDING`-base cross-check; revisit only if they ever disagree.

---

## Phase 20 — Cash-Affecting Transfers + Cash-Model Validation (PARTIALLY BUILT)
**Goal:** Capture external cash inflows correctly and finally **validate `reconstruct_cash` against a
post-period Holdings snapshot** (the long-standing Phase 3 / Decision 19 double-count hazard).
**Status: parser + cash logic built** (`Funds Received → CASH_IN`, `Current Year Contribution →
CONTRIBUTION_INFO` excluded from cash math). The real-data snapshot reconciliation remains **deferred**
until a post-Jun Holdings CSV is available.

### Decisions (locked with user — "track inflows, de-dup contributions")
- **`FundTransfers / Funds Received` → `CASH_IN`.** It is the *only* record of the wire — the $30K wires
  have **no** matching `990156937` sweep `Deposit` row (verified in the 04–06/2026 file) — so omitting it
  under-reconstructs cash.
- **`FundReceipts / Current Year Contribution` → ignore for cash.** The same money already appears as an
  `IIAXX` `Other/Deposit` (e.g. the $8,000 contribution on 1/5 → IIAXX deposit on 1/6), so counting both
  **double-counts**. Keep the row in Transactions for the record only.

### Approach
- Parser: `FundTransfers/Funds Received → CASH_IN`; `FundReceipts/Current Year Contribution →
  CONTRIBUTION_INFO` (recorded, excluded from cash math).
- `reconstruct_cash`: credit `Funds Received` to the account's cash account (CMA → `990156937` sweep;
  Roth → `IIAXX`) since the row's `Symbol/CUSIP #` is `--`; confirm it is not *also* netted into a sweep row.
- **Amends Decision 19:** external contributions are now *partially* separable (explicit transfer rows),
  so the cash balance can include them; return-vs-contribution attribution stays out of scope.

### Deliverables
- `portfolio/parsers/activity_parser.py` (TX_TYPE_MAP additions)
- `portfolio/engine/cash.py` (`reconstruct_cash` includes `Funds Received`; account → cash-account resolution)
- `tests/test_activity_parser.py`, `tests/test_cash.py`

### Validation
- Unit: a `Funds Received` row raises reconstructed cash by its amount on the right cash account; a
  `Current Year Contribution` paired with its `IIAXX` deposit nets to **one** inflow (no double-count).
- Real data (**deferred — needs a post-Jun Holdings CSV**): reconstruct CMA/Roth cash through 06/2026 and
  reconcile to the snapshot sweep/IIAXX balances; drift ≈ 0.
```bash
python3 -m pytest tests/test_activity_parser.py tests/test_cash.py -q
```

---

## Phase 21 — Stateful Ongoing Operation (replay from the sheet, not the run) (PLANNED)
**Goal:** Make month-to-month operation match the documented model: **bootstrap an account once
(Unrealized + Holdings), then each subsequent run needs only the new activity CSV(s)** (plus an optional
current Holdings, used purely for verification/snapshot). **Status: not yet built.**

### The gap (why the current design forces "re-feed everything")
`compute_holdings`, `reconstruct_cash`, and the performance functions replay `all_transactions` =
**only the files parsed this run** (`runner.py`). The Transactions sheet is written to and read for
**write-dedup keys only** (`load_existing_transaction_keys` returns tuples, never rehydrated into
`Transaction` objects). Two consequences break incremental feeding:
1. **Bootstrap lots vanish.** A run without the Unrealized file has **no `INIT_BUY` rows** in the replay
   (they sit in the sheet, un-reloaded), so holdings compute from that month's activity alone →
   understated positions and latent oversells. Correct results today require re-feeding **Unrealized +
   every activity file since `init_date`** together.
2. **Cash double-counts on a current Holdings.** `bootstrap_cash` (the replay's **starting** balance) and
   `snapshot_cash` (the reconcile target) are the **same object** — the fed Holdings — while the cutoff
   is frozen at `init_date`. Feed a period-end Holdings and the replay re-adds the whole period's flow on
   top of a balance that already contains it (Non-Obvious #29 territory: the safe cash model assumes the
   start is the init-date balance).

### Decisions (to lock with user)
- **Sheet is the source of truth for replay.** After writing this run's new (deduped) rows, **reload the
  full Transactions tab into `Transaction` objects** and compute holdings/cash/performance from that set —
  not from the run's files. Write-then-reload guarantees one source and no double-add of this-run rows.
- **Freeze bootstrap cash in `account_state.json`.** At bootstrap, persist each account's **init-date cash
  balance** (per cash account) alongside `init_date`/`bootstrap_source_file`, set once, never overwritten
  (mirrors the `init_date` rule). `reconstruct_cash` starts from the **persisted** balance.
- **A fed Holdings becomes snapshot-only.** It no longer seeds the replay; it feeds equity verification +
  the Cash `snapshot`/`Drift` columns. Drift then reconciles to ≈0 against a **same-dated** snapshot
  instead of measuring staleness — this also **discharges the deferred Phase 20 real-data validation**.
- **Onboarding unchanged:** a new account still needs BOTH Unrealized (lots + init_date) and Holdings
  (now → persisted bootstrap cash). Ongoing = new activity CSV(s) + optional current Holdings.

### Approach
- New reader `load_all_transactions(sh) -> list[Transaction]` in `sheets/writer.py`, rehydrating by
  **column position** via `TRANSACTIONS_KEYS` (Non-Obvious #22) — must round-trip dates (ISO→`date`),
  numbers (fixed-decimal→`float`), blanks→`None`, and every `tx_type` (INIT_BUY, SPLIT,
  CASH_TRANSFER_IN, CONTRIBUTION_INFO) with correct amount **signs**.
- Reorder `runner`: parse → write (dedup) → **reload full sheet** → compute from reloaded set.
- Extend `account_state` schema + bootstrap step to persist `bootstrap_cash` (per cash account); update
  `save/load_account_state`.
- `reconstruct_cash`: start balance from persisted `account_state`, not the fed Holdings; `snapshot_cash`
  = fed Holdings (if any).
- **Migration:** already-bootstrapped accounts have `init_date` but no `bootstrap_cash`. First run under
  the new scheme seeds it from the **bootstrap-date** Holdings (`COB == init_date`); if that file isn't
  present, log + skip cash reconciliation for that account until it is (never guess).

### Deliverables
- `portfolio/sheets/writer.py` (`load_all_transactions`, position-based rehydration)
- `portfolio/runner.py` (write-then-reload ordering; bootstrap-cash from state; Holdings → snapshot-only)
- `portfolio/drive/archiver.py` or account-state module (persist/read `bootstrap_cash`)
- `portfolio/engine/cash.py` (start from persisted bootstrap; snapshot decoupled)
- `tests/test_writer.py` (round-trip), `tests/test_runner*.py` / `tests/test_cash.py`

### Validation
- Unit: `load_all_transactions` round-trips a written sheet to **equal** `Transaction` objects across all
  types (INIT_BUY/SPLIT/CASH_TRANSFER_IN/None fields), by position not header.
- Unit: an "ongoing" run fed **only new activity** (bootstrap lots + prior activity from the sheet)
  reproduces the same holdings as the all-at-once run; a current-dated Holdings does **not** inflate cash
  (bootstrap frozen in state, snapshot separate); `bootstrap_cash` never overwritten once set.
- Real data — **two-stage replay of the 01–06/2026 set**: stage 1 bootstrap (Unrealized + Holdings
  `1/30`); stage 2 feed **only** the two activity files; assert holdings/cash equal the all-in-one run
  (46,235.92 / 33,238.58 for the wire accounts, etc.). End-to-end Drift≈0 against a period-end Holdings
  stays **deferred until a post-Jun Holdings CSV exists**.
```bash
python3 -m pytest tests/test_writer.py tests/test_cash.py -q
```

### Risk
Rehydration fidelity is load-bearing: any type/sign drift on read-back silently corrupts every downstream
tab. The `test_dedup_readback_is_position_based` guard (Non-Obvious #22) is the anchor; extend it to full
object round-trip. Reordering the runner also moves holdings/cash/performance **after** the write — verify
no consumer relied on the pre-write ordering.

---

## Phase 22 — In-Sheet Instructions + Tab Presentation (ordering, merge, hide) (PLANNED)
**Goal:** Make the sheet self-explanatory and tidy for non-technical family users: a bilingual
**Instructions** tab (driven by a **codebase source of truth** so it can't silently drift from the code),
a **fixed, sensible tab order**, the **Stock Metrics + Price History** tabs merged into one, and the
backing/debug tabs (**Performance By Year**, **Run Log**) **hidden**. **Status: not yet built.**

### Decisions (locked with user)
- **Canonical tab order, re-applied every run** (tabs currently land in creation order):
  | # | Tab | Note |
  |---|-----|------|
  | 0 | **Instructions** | visible, leftmost |
  | 1 | Transactions | |
  | 2 | Holdings | |
  | 3 | Performance | lifetime; readable, stays visible (also backs Compare) |
  | 4 | **Stock Metrics** | Price History **merged in** (see below) |
  | 5 | Performance Compare | |
  | 6 | Watchlist | editable input |
  | 7 | Cash | |
  | — | Performance By Year | **hidden** — backing data for Performance Compare (+ pivot source) |
  | — | Run Log | **hidden** — debug only |
- **Merge Price History into Stock Metrics.** Both are one-row-per-symbol over the same symbol universe
  (recorded ∪ watchlist), clear-and-rewrite. Merged columns:
  `Symbol | P/E | Div Yield | ROE(1Y…10Y) | Net Income | Book Value | Current Price | Today | 1Y…5Y Ago |
  As Of Date`. `Current Price` stays a `GOOGLEFINANCE` formula; anniversary prices stay static values
  (Stock Metrics already mixes formula+value cells). The standalone **Price History tab is removed**; the
  Drive `price_history_cache.json` (feeds the Performance year math) is **untouched** — this merges only
  the display tabs.
- **Performance By Year stays (Performance Compare `INDEX/MATCH`es it) but is HIDDEN.** Long-format backing
  data; also the right **source for a hand-built per-year pivot** if the user wants one. Never delete while
  Performance Compare exists.
- **Run Log → HIDDEN, last, same spreadsheet.** Keep writing it every run (unchanged content); just move
  it last + hide. (No separate spreadsheet / no external log file.)
- **One Instructions tab, stacked, Traditional Chinese FIRST, then English**, placed **first / leftmost**.
- **User supplies the Traditional Chinese.** The codebase source ships **English + `zh` placeholders**;
  the user fills the `zh` strings. Tests must NOT fail on empty `zh` (warn/allow), only on missing tabs.
- **Codebase file is the source of truth.** A single reference module holds each tab's purpose/how-to
  (EN + ZH); `write_instructions` renders it, and a **drift guard** test ties it to the real tab set so a
  tab added/removed/renamed in code fails CI until the guide is updated (the user's core ask).

### Approach
- **Merge Price History into Stock Metrics.** Fold `write_price_history`'s anniversary columns into
  `write_stock_metrics` (one write, one row per symbol), drop the standalone `TAB_PRICE_HISTORY` tab and
  its writer; keep `histories` fetching + the Drive cache exactly as-is. Update `STOCK_METRICS_HEADERS` and
  the `As Of Date`-last invariant. (If the old Price History tab exists on a deployed sheet, delete it once
  on migration.)
- **New source module** `portfolio/sheets/tab_guide.py`: a `TabDoc` dataclass (`tab` = the real `TAB_*`
  constant, `kind` = output|input|hidden, `en`, `zh`) and an ordered `TAB_GUIDE: list[TabDoc]` covering
  every tab in canonical order, plus a short top-of-page "how the workflow works" blurb (drop CSVs → run →
  read tabs; which two tabs are editable). Emphasize the **editable** surfaces (`Watchlist` = config list
  of tickers to track even if unheld → flows into Stock Metrics; `Performance Compare` = pick symbols in
  the row-1 dropdowns).
- **`write_instructions(sh, guide)`** in `writer.py`: **clear-and-rewrite each run** (it has NO user
  state and MUST track code — the deliberate OPPOSITE of the set-up-once `Watchlist`/`Performance Compare`
  tabs, see #23/#30). Render ZH block, a divider, then EN block, as **values**.
- **`apply_tab_order(sh)`**: one pass after all writes that sets each tab's `index` to the canonical order
  and `hidden` = True for `Performance By Year` + `Run Log`, via a single `batch_update` of
  `updateSheetProperties` requests (idempotent; tolerate already-hidden / missing optional tabs).
- **Drift guard**: derive `WRITTEN_TABS` from a canonical order list (single source for both ordering and
  the guard); test asserts `{d.tab for d in TAB_GUIDE} == WRITTEN_TABS`. Adding a `write_*` tab without a
  `TabDoc` (or dropping one) fails the test.

### Deliverables
- `portfolio/sheets/tab_guide.py` (`TabDoc`, `TAB_GUIDE` — EN authored, ZH placeholders; canonical order)
- `portfolio/sheets/writer.py` (`write_instructions`, `apply_tab_order`, merged `write_stock_metrics`,
  remove `write_price_history`/`TAB_PRICE_HISTORY`, `WRITTEN_TABS`)
- `portfolio/runner.py` (call `write_instructions`; drop the separate price-history write; `apply_tab_order`
  as the final Sheets step)
- `tests/test_writer.py` / `tests/test_tab_guide.py`
- `CLAUDE.md` (tab-structure table: add `Instructions`, merge Price History into Stock Metrics, mark
  `Performance By Year` + `Run Log` hidden; column-schema update; Non-Obvious for the clear-and-rewrite
  exception + drift guard)

### Validation
- Unit: merged `write_stock_metrics` emits the fundamentals **and** anniversary-price columns in one row
  per symbol, `As Of Date` last, `Current Price` still a `GOOGLEFINANCE` formula.
- Unit: `write_instructions` renders **ZH before EN**, contains each tab's name + how-to, writes values
  (not formulas); it is **clear-and-rewrite** (safe to re-run, no user state).
- Unit: `apply_tab_order` sets the canonical `index` order and hides `Performance By Year` + `Run Log`
  (idempotent when already ordered/hidden; skips optional tabs absent this run).
- Unit (**drift guard**): `TAB_GUIDE` covers exactly `WRITTEN_TABS`; an undocumented new tab fails.
- Unit: empty `zh` placeholders are **allowed** (guide still renders; guard passes).
- Manual: run against the sheet; confirm order matches the table, Stock Metrics carries price columns,
  Performance By Year + Run Log hidden but present.
```bash
python3 -m pytest tests/test_writer.py tests/test_tab_guide.py -q
```

### Risk
`write_instructions` is the one Python tab that MUST clear-and-rewrite (opposite of #23/#30) — guard it so
nobody "protects" it into a stale no-op. The drift guard only catches tab-set changes, not prose going
stale within a tab (e.g. a column meaning changes) — content review stays a human step. Hiding
`Performance By Year`/`Run Log` must never stop them being written (Compare backing data + debug lifeline).
The Stock Metrics merge shifts column positions — re-check any position-based reader (though Stock Metrics
is clear-and-rewrite, so lower risk than the Transactions tab).

### Implementation notes (for the session that builds this)
- **Format the Instructions tab — don't just dump values.** With ZH stacked first, a flat values grid reads
  cramped. Apply light formatting so it reads like a document: **bold section headers** (a title per tab),
  a **blank spacer row between tabs**, and a clear ZH/EN divider. Do it via `batch_update`
  `repeatCell`/`updateCells` `textFormat.bold` on the header rows after writing the values.
- **The Stock Metrics merge shifts column positions — verify no position-based reader depends on the old
  layout before shipping.** Stock Metrics is clear-and-rewrite (lower risk than the position-load-bearing
  Transactions tab per #22), but confirm nothing reads Stock Metrics / the removed Price History tab by
  column index, and update `STOCK_METRICS_HEADERS` + the `As Of Date`-last invariant together.

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

Post-MVP enhancements (sequence by dependency, not number):
Phase 9 (Human headers, dedup-safe reads)
    └── Phase 10 (Metrics scope + Watchlist)  ← uses Phase 9's TRANSACTIONS_KEYS reads
            └── Phase 11 (5-yr Price History)  ← consumes Phase 10's symbol set
                    ├── Phase 12 (Composition)       ← REMOVED (use native sheet charts)
                    ├── Phase 13 (Performance)       ← + FIFO as-of + transactions (backing data)
                    │       └── Phase 16 (Performance Compare)  ← interactive cards on Phase 13's tabs
                    ├── Phase 14 (Opportunity Cost)  ← REMOVED (re-evaluate)
                    └── Phase 15 (Price History reshape)  ← anniversary snapshots, rows = symbols

Cross-cutting (corporate actions + metrics depth; sequence by dependency):
Phase 17 (ROE by calendar year, 10-yr capacity)   ← supersedes Phase 4 ROE fields [built]
Phase 18 (Ticker renames, TICKER_RENAMES)          ← fixes rename oversell in Phase 3 FIFO [built]
Phase 19 (Stock splits/dividends, in-place lots)   ← extends Phase 2 parser + Phase 3 FIFO [built]
Phase 20 (Cash transfers + cash-model validation)  ← extends Phase 2 parser + Phase 3 cash [built; snapshot reconcile deferred]
Phase 21 (Stateful ongoing: replay from the sheet)  ← reworks Phase 5 read-back + Phase 6 account_state [planned]
Phase 22 (Instructions + tab order/merge/hide)      ← usability/presentation layer on Phase 5 tabs [planned]
Phase 8 (Hardening) is orthogonal — do it before or after 9–11.
```

---

## Definition of "Done" (MVP)

1. Hank or Mom drops Merrill CSV(s) into the shared Drive upload folder
2. Opens the Colab notebook and clicks Run All
3. Google Sheet is updated: new Transactions rows, current Holdings, Stock Metrics with GOOGLEFINANCE prices, Run Log entry
4. CSV moved to processed/ folder
5. No manual steps beyond (1) downloading the CSV from Merrill and (2) clicking Run All in Colab
