# Investment Sheet Manager — Claude Memory Bank

## Project Goal

Build a lightweight family portfolio management workflow using Merrill Edge CSV exports as source of truth. Hank and his mom drop downloaded CSVs into a shared Google Drive folder, then open a Google Colab notebook to manually trigger processing. The notebook pulls the latest engine code from GitHub, processes all pending CSVs, and writes outputs to Google Sheets.

**Layer assignments:**
- **Google Drive** — intake (upload folder) + archive (Processed / Failed folders) + yfinance cache
- **GitHub** — version-controlled Python engine (this repo)
- **Google Colab** — manual trigger / control panel only (no logic lives here)
- **Google Sheets** — shared output + lightweight settings surface

**GitHub repo:** `https://github.com/humble-methods/investment-sheet-manager`
**Drive folder:** `https://drive.google.com/drive/folders/<DRIVE_ROOT_FOLDER_ID>?usp=drive_link`
**Drive folder ID:** `<DRIVE_ROOT_FOLDER_ID>`

---

## Repo Structure

```
investment-sheet-manager/
├── CLAUDE.md                        # This file
├── BUILD_PLAN.md                    # Phased implementation plan
├── README.md
├── requirements.txt
├── .gitignore
├── portfolio/
│   ├── __init__.py
│   ├── config.py                    # Symbol overrides, Drive IDs, Sheet ID, constants
│   ├── models.py                    # Transaction, Lot, Position dataclasses
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── utils.py                 # parse_amount(), parse_date(), clean_symbol()
│   │   ├── activity_parser.py       # PRIMARY: parses activity CSV → List[Transaction]
│   │   ├── unrealized_parser.py     # BOOTSTRAP: parses unrealized CSV → List[Transaction] (INIT_BUY)
│   │   └── holdings_parser.py       # VERIFICATION: parses holdings CSV snapshot
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── fifo.py                  # FIFO lot tracking → List[Lot]
│   │   └── holdings.py              # Compute current holdings from transaction history
│   ├── market/
│   │   ├── __init__.py
│   │   ├── symbol_overrides.py      # Ticker normalization (BRKB→BRK-B, etc.)
│   │   └── yfinance_client.py       # yfinance wrapper + Google Drive JSON cache
│   ├── metrics/
│   │   ├── __init__.py
│   │   └── fundamentals.py          # ROE + P/E + dividend yield from yfinance
│   ├── sheets/
│   │   ├── __init__.py
│   │   └── writer.py                # Google Sheets output via gspread
│   ├── drive/
│   │   ├── __init__.py
│   │   └── archiver.py              # Move CSVs to Processed/ or Failed/
│   └── runner.py                    # Main orchestrator / entry point
├── tests/
│   ├── __init__.py
│   ├── sample_data/                 # Redacted sample CSVs for unit tests
│   ├── test_activity_parser.py
│   ├── test_unrealized_parser.py
│   ├── test_fifo.py
│   └── test_holdings.py
└── notebook/
    └── portfolio_update.ipynb       # Colab control panel
```

---

## Bootstrap vs. Ongoing Operation

### Bootstrap (first run)
The accounts already have positions. We cannot reconstruct full history from activity CSVs alone. Instead:

1. User drops an **Unrealized CSV** into the upload folder alongside any activity CSVs.
2. The engine detects the Unrealized CSV and converts each lot into a synthetic `INIT_BUY` transaction dated to the lot's acquisition date.
3. These `INIT_BUY` transactions are written to the Transactions sheet with `source_file = "INIT:<filename>"`.
4. Subsequent activity CSVs are layered on top chronologically.

**Rule:** If an `INIT_BUY` already exists in the Transactions sheet for a given `(account_number, symbol, acquisition_date, quantity, unit_cost)`, skip it. Never double-import.

### Ongoing
- User drops activity CSVs (one or more) into the upload folder.
- Engine parses, deduplicates against existing Transactions sheet, appends new rows.
- Holdings are always recomputed from the full Transactions history (replay from scratch each run).

---

## Merrill Edge CSV Schemas

All four report types export from Merrill Edge. Numbers use comma-thousands separators and **parentheses for negatives** (e.g., `(3,211.38)` = -3211.38). Dates are `M/D/YYYY`.

### 1. Activity CSV (Primary Input — Ongoing)
**Filename pattern:** `PendingAndSettledActivity_MMYYYY_MMYYYY.csv` or `Settled_MMYYYY_MMYYYY.csv`

| Column | Notes |
|--------|-------|
| Trade Date | M/D/YYYY |
| Settlement Date | M/D/YYYY |
| Pending/Settled | `Pending` or `Settled` |
| Account Nickname | Always `--` (ignore) |
| Account Registration | `CMA-Edge`, `Roth IRA-Edge` (account type label) |
| Account # | Canonical account identifier (e.g., `11A-00003`) |
| Type | Transaction category (see mapping below) |
| Description 1 | Sub-type (see mapping below) |
| Description 2 | Full security name + boilerplate routing text (ignore boilerplate) |
| Symbol/CUSIP # | Ticker for equities; CUSIP-like code for cash/other |
| Quantity | Negative for sales and withdrawals |
| Price ($) | `--` for non-trade rows |
| Amount ($) | Dollar amount; parentheses = negative |

**IMPORTANT — skip Pending rows.** Only process rows where `Pending/Settled == "Settled"`. Pending transactions will appear as Settled in the next activity CSV download. Do not attempt to update pending→settled status; just ignore pending rows entirely.

**Transaction type mapping (Type → Description 1 → canonical type):**

| Type | Description 1 | Canonical Type | Notes |
|------|--------------|----------------|-------|
| SecurityTransactions | Purchase | BUY | |
| SecurityTransactions | Sale | SELL | Qty is negative |
| SecurityTransactions | Interest | REINVEST | Roth IRA money market (IIAXX) |
| DividendAndInterest | Dividend | DIVIDEND | |
| DividendAndInterest | Foreign Dividend | DIVIDEND | |
| DividendAndInterest | Bank Interest | INTEREST | Cash sweep interest |
| Other | Deposit | CASH_IN | ML Direct Deposit Program |
| Other | Withdrawal | CASH_OUT | |
| Other | Depository Bank (ADR) Fee | ADR_FEE | |
| Other | Foreign Tax Withholding | TAX_WITHHOLDING | Negative amount |

**What to skip / treat as cash (not equity positions):**
- `Symbol/CUSIP # = 990156937` → ML DIRECT DEPOSIT PROGRM (cash sweep). Classify activity as CASH_FLOW. Do NOT create equity lots.
- `Symbol/CUSIP # = IIAXX` → Bank of America RASP (Roth IRA money market). Treat as CASH_IRA, skip equity processing.

**Deduplication key for activity transactions:**
`(trade_date, settlement_date, account_number, tx_type, symbol, quantity, amount)`
Before writing to Transactions sheet, fetch existing rows and exclude any that match this key. Activity CSVs routinely overlap date ranges across downloads.

### 2. Unrealized CSV (Required for Bootstrap)
**Filename pattern:** `Unrealized_AllAccounts_MMYYYY.csv`

| Column | Notes |
|--------|-------|
| COB Date | As-of date (used as snapshot date for init) |
| Security # | Internal code (ignore) |
| Symbol | Ticker |
| CUSIP # | CUSIP; `990156937` for cash, `55499U915` for IIAXX |
| Security Description | Full name |
| Account Nickname | `--` |
| Account Registration | Account type label |
| Account # | Canonical account identifier |
| Acquisition Date | Original purchase date (per lot) — use as INIT_BUY trade_date |
| Quantity | Shares in this lot |
| Unit Cost ($) | Per-share cost for this lot |
| Cost Basis ($) | Total cost for this lot |
| Value ($) | Current market value |
| Unrealized Gain/Loss ($) | P&L for this lot |
| Unrealized Gain/Loss (%) | P&L % for this lot |
| Short/Long | `(Short Term)` or `(Long Term)` |

**Conversion to INIT_BUY transactions:**
Each row in the Unrealized CSV (excluding cash rows) becomes one `Transaction` with:
- `tx_type = "INIT_BUY"`
- `trade_date = Acquisition Date`
- `settlement_date = Acquisition Date` (same)
- `status = "Settled"`
- `quantity = Quantity` (positive)
- `price = Unit Cost`
- `amount = -(Cost Basis)` (negative — money left the account)
- `source_file = "INIT:<filename>"`

**Dedup key for INIT_BUY:** `(account_number, symbol, trade_date, quantity, price)` — skip if already in Transactions sheet.

### 3. Holdings CSV (Verification Only)
**Filename pattern:** `Holdings_AllAccounts_MMYYYY.csv`

| Column | Notes |
|--------|-------|
| COB Date | As-of date |
| Security # | Internal Merrill code (ignore for matching) |
| Symbol | Ticker; `--` for cash positions |
| CUSIP # | CUSIP; `990156937` for cash sweep |
| Security Description | Full name |
| Account Nickname | Always `--` |
| Account Registration | Account type label |
| Account # | Canonical account identifier |
| Quantity | Shares held |
| Price ($) | Current price |
| Value ($) | Current market value |
| Unrealized Gain/Loss ($) | `--` for ETFs/funds |
| Unrealized Gain/Loss (%) | `--` for ETFs/funds |
| Cumulative Investment Return ($) | ETFs/funds only |
| Cumulative Investment Return (%) | ETFs/funds only |
| Accrued Interest ($) | Always `--` for equities |

**Use:** Cross-validate computed holdings quantities against Merrill's snapshot. Flag discrepancies in the Run Log.

### 4. Realized CSV (Optional Input)
**Filename pattern:** `Realized_AllAccounts_MMYYYY.csv`

| Column | Notes |
|--------|-------|
| Security | Internal code (not ticker) |
| Security Description | Full name |
| Account Nickname | Always `--` |
| Account Registration | Account type label |
| Account # | Canonical account identifier |
| Acquisition Date | M/D/YYYY |
| Liquidation Date | M/D/YYYY |
| Quantity | Shares sold |
| Acquisition Price ($) | Per-share cost |
| Acquisition Cost ($) | Total cost basis |
| Liquidation Price ($) | Per-share sale price |
| Liquidation Amount ($) | Total proceeds |
| Gain/Loss ($) | Realized P&L |
| Short/Long | `(Short Term)` or `(Long Term)` — literal strings, not negatives |

**Use:** Historical context only. Not used for FIFO computation.

---

## Special Symbol Handling

All symbol normalization lives in `portfolio/market/symbol_overrides.py`.

### Cash / Non-Equity Positions (skip from holdings, no yfinance lookup)
| Symbol/CUSIP in CSV | Description | Treatment |
|---------------------|-------------|-----------|
| `990156937` | ML DIRECT DEPOSIT PROGRM | Cash sweep — skip from equity processing |
| `IIAXX` | Bank of America RASP | Roth IRA money market — skip from equity processing |

### Ticker Normalization (Merrill → yfinance)
| Merrill Symbol | yfinance Symbol | Notes |
|---------------|-----------------|-------|
| `BRKB` | `BRK-B` | Berkshire Hathaway B |

*Add more overrides here as discovered.*

### ADR Symbols
Merrill uses standard US ticker symbols for ADRs (`IX`, `TM`, `LYG`, `TSM`, etc.). These work directly with yfinance.

---

## Google Drive Folder Structure

**Root folder ID:** `<DRIVE_ROOT_FOLDER_ID>`

```
[Shared Drive Root]/          # ID: <DRIVE_ROOT_FOLDER_ID>
├── upload/                   # ID: <DRIVE_UPLOAD_FOLDER_ID>
├── processed/                # ID: <DRIVE_PROCESSED_FOLDER_ID>
├── failed/                   # ID: <DRIVE_FAILED_FOLDER_ID>
└── cache/                    # ID: <DRIVE_CACHE_FOLDER_ID>
    ├── yfinance_cache.json   # yfinance fundamentals cache (24hr TTL)
    └── account_state.json    # per-account init dates + bootstrap source files (permanent)
```

---

## Google Sheets Tab Structure

**Output tabs (written by Python, do not manually edit):**

| Tab | Description |
|-----|-------------|
| `Transactions` | All normalized transactions (INIT_BUY + activity). One row per event. |
| `Holdings` | Current position summary per account. Derived by replaying Transactions. |
| `Cash` | Per-account cash (CMA sweep 990156937 / Roth IIAXX): reconstructed vs snapshot + drift. |
| `Stock Metrics` | Combined metrics + ROE per ticker. Prices via GOOGLEFINANCE formulas. |
| `Run Log` | One row per run: timestamp, files processed, errors, holdings delta, skipped accounts, cash reconciliation. |

**Editable tabs (human-maintained):**

| Tab | Description |
|-----|-------------|
| `Watchlist` | Optional: tickers to track even if not held. |

Note: configuration (Drive folder IDs, spreadsheet ID, toggles) lives in `portfolio/config.py`,
not a Settings tab — keep operational config in code. Per-account init dates are runtime state
in Drive `cache/account_state.json`, not the sheet.

---

## Google Sheets Column Schemas

### Transactions tab
`trade_date | settlement_date | status | account_number | account_registration | tx_type | description | symbol | quantity | price | amount | source_file`

### Holdings tab
`as_of_date | account_number | account_registration | symbol | quantity | avg_cost | cost_basis`

Note: Current price and market value are NOT stored here — they're computed live in Google Sheets using GOOGLEFINANCE formulas in adjacent columns.

### Cash tab
`as_of_date | account_number | account_registration | owner | cash_account | reconstructed | snapshot | drift`

`cash_account` is `990156937` (CMA sweep) or `IIAXX` (Roth). `reconstructed` replays activity from
the bootstrap; `snapshot` is from the latest Holdings CSV (blank if none this run); `drift` flags mismatch.

### Stock Metrics tab
Python writes these columns (from yfinance, updated each run):
`as_of_date | symbol | pe_ratio | dividend_yield | roe_current | roe_1y | roe_2y | roe_3y | roe_4y | net_income | book_value`

Google Sheets GOOGLEFINANCE formulas live in additional columns (not written by Python):
- Current price: `=GOOGLEFINANCE(B2, "price")`
- 52-week high: `=GOOGLEFINANCE(B2, "high52")`
- 52-week low: `=GOOGLEFINANCE(B2, "low52")`
- 5-year price history: Written as a separate range using `=GOOGLEFINANCE(B2, "price", TODAY()-5*365, TODAY(), "WEEKLY")`

### Run Log tab
`run_timestamp | files_processed | init_rows_added | transactions_added | accounts_skipped | errors | holdings_changed | cash_reconciliation | duration_sec | notes`

---

## Key Architectural Decisions

### Bootstrap via Unrealized CSV (per account, with init date)
- Existing lots are seeded as synthetic INIT_BUY transactions using the Unrealized CSV
- Acquisition Date from each unrealized lot becomes the INIT_BUY trade_date
- This preserves FIFO ordering: when a sell happens, the correct original lot is depleted
- **Per-account init date:** each account is bootstrapped from its FIRST Unrealized
  intake; that file's **COB Date becomes the account's permanent init/cutoff date**.
  Stored in Drive `cache/account_state.json` (`{account: {init_date, bootstrap_source_file}}`),
  set once, never overwritten. The account roster is dynamic — new accounts onboard over time.
- **Cash bootstrap needs the Holdings CSV, not Unrealized.** The cash sweep (990156937)
  and Roth IIAXX are absent from Unrealized. Onboarding a new account therefore requires
  BOTH an Unrealized intake (equity lots + init date) and a Holdings intake (cash starting balance).
- **Un-bootstrapped accounts are skipped + flagged**, never crash-processed. Activity for an
  account with no `account_state` entry is deferred entirely until its Unrealized/Holdings arrive.

### Activity CSV is the Primary Source of Truth (Ongoing)
- Full transaction history replayed from INIT_BUY + subsequent activity CSVs
- **Cutoff:** ignore activity with `trade_date <= account_init_date` (already in the snapshot)
- Holdings CSV used for verification (equity quantities) AND cash bootstrap/reconciliation
- FIFO lots derived from this transaction history
- Realized CSV is NOT ingested — activity is a strict superset for the ongoing flow

### Only Process Settled Transactions
- Rows with `Pending/Settled == "Pending"` are skipped entirely
- They will reappear as Settled in the next activity CSV download
- No pending-to-settled update logic needed

### Deduplication (Critical)
- Activity CSVs regularly overlap date ranges
- Dedup key for activity: `(trade_date, settlement_date, account_number, tx_type, symbol, quantity, amount)`
- Always fetch existing Transactions sheet rows before appending; skip duplicates
- **Normalize all key fields to canonical strings** (dates ISO, numbers fixed-decimal) on
  BOTH write and read-back — values round-tripped through Sheets won't compare as raw floats.
- **Identical activity fills** (same key within a settled file) are **flagged for human review**,
  not auto-merged — a genuine platform-split fill would otherwise be lost.
- **INIT_BUY is different:** do NOT inter-dedup INIT rows within a bootstrap file. Identical
  lots are real (e.g. AXP `11A-00003`: two 5 sh @ $164.35 / 11-26-2021). Re-import protection
  is by `source_file` identity only — each Unrealized row is a distinct lot.

### FIFO Cost Basis
- Cost basis tracked per lot per account (acquisition_date, quantity, unit_cost)
- Sells deplete oldest lots first within the same account
- Holdings recomputed from scratch each run by replaying full transaction history

### Price Data via GOOGLEFINANCE (Not Python)
- Current prices and historical price series are NOT fetched by Python
- Google Sheets GOOGLEFINANCE formulas handle all price data — faster, always fresh
- Python only writes fundamental data (ROE, P/E, dividend yield) from yfinance
- ROE history: 4 years of annual ROE data written to Stock Metrics tab

### yfinance Caching
- Cache stored in Google Drive as `cache/yfinance_cache.json`
- Cache key: ticker symbol (normalized)
- Cached fields: pe_ratio, dividend_yield, roe (current + 4 prior years), net_income, book_value, fetched_at
- Cache TTL: 24 hours
- On yfinance failure: keep stale data, log warning

### CSV File Movement
- On success: move to `processed/YYYYMMDD_HHMMSS_<original_filename>`
- On failure: move to `failed/YYYYMMDD_HHMMSS_<original_filename>`
- Never delete originals

---

## Number Parsing

`parse_amount()` must handle all Merrill number formats:
- `""` or `"--"` → `None`
- `"(3,211.38)"` → `-3211.38`
- `"3,211.38"` → `3211.38`
- `"128.46"` → `128.46`
- `"19"` (integer-like) → `19.0`

---

## Non-Obvious Behaviors

1. **BRKB vs BRK-B**: Merrill exports as `BRKB`. yfinance requires `BRK-B`. Always normalize before any yfinance call.

2. **Cash rows in activity**: `Deposit`/`Withdrawal` rows with `Symbol = 990156937` are cash flow into/out of the ML cash sweep. NOT stock transactions.

3. **Cash amount sign convention**: A `Deposit` row with amount `(19.00)` means $19 arrived in the account (parentheses = outflow from Merrill's perspective, inflow for the investor). A `Withdrawal` with positive amount means cash left.

4. **ADR fees are negative**: `Depository Bank (ADR) Fee` rows have negative amounts. They reduce cash but are not equity transactions.

5. **Foreign tax withholding**: Also negative amounts. Reduces cash. Separate from dividends.

6. **Quantity field for dividends**: Is `--` (not a number). Parse as `None`.

7. **Price field for dividends/cash**: Is `--`. Parse as `None`.

8. **Short/Long parentheses in Realized CSV**: `(Short Term)` and `(Long Term)` are literal label strings — NOT negative numbers. Parse as strings, strip parens.

9. **Description 2 boilerplate (two variants)**: Some rows lead with `ACTUAL PRICES, REMUNERATION… UPON REQUEST. CLIENT ENTERED…`, others go straight to `CLIENT ENTERED…`. Strip from **whichever marker appears first**; everything before it is the useful security name.

10. **Settlement date lag**: Most trades settle T+1 or T+2. Use `trade_date` as canonical date for FIFO sequencing.

11. **Fractional shares**: `COF` appears with `30.576` and `50.424` shares. All quantity parsing must use `float`, not `int`.

12. **IIAXX reinvest**: Roth IRA shows IIAXX (money market) with tiny interest reinvestments (e.g., `0.07` shares). Skip from equity holdings.

13. **INIT_BUY vs real BUY**: The FIFO engine must treat `INIT_BUY` the same as `BUY` when building lots. The distinction is only for source tracking in the Transactions sheet.

14. **Unrealized CSV per-lot rows**: A single position may appear as multiple rows in the Unrealized CSV — one per lot (different acquisition dates). Each becomes a separate INIT_BUY transaction to preserve FIFO accuracy.

15. **Trailing whitespace in Type fields**: The header is literally `"Description 1 "` and values ship as `"Purchase "`, `"Sale "` (trailing space). **`.strip()` Type and Description 1 before any TX_TYPE_MAP lookup** or every trade falls through to UNKNOWN.

16. **Identical INIT_BUY lots are real**: A single account can hold byte-identical lots (e.g. AXP `11A-00003`: two 5 sh @ $164.35 / 11-26-2021). Keep both; never inter-dedup INIT rows within a file. (Identical *activity* fills, by contrast, are flagged for human review.)

17. **account_number is the only canonical key**: `account_registration` is NOT unique (six accounts share `CMA-Edge`). Account numbers can differ by one character (`11A-00003` vs `11A-00004`) — treat as exact strings, never normalize.

18. **Cash sweep absent from Unrealized**: `990156937` (CMA) and `IIAXX` (Roth) appear in the Holdings CSV but NOT the Unrealized CSV. Bootstrap cash from Holdings. They are dollar-denominated (~$1 NAV); never create equity lots or do yfinance lookups for them.

19. **Sweep Deposit/Withdrawal are netted trade settlement**: The cash-sweep rows already net buys/sells/dividends as they settle (6/3 trades net ≈ +$41,414 → $41,412 sweep deposit on 6/5). Summing both trade amounts AND sweep rows double-counts — pick one model and validate against a real multi-month set. External contributions are indistinguishable from internal sweeps.

20. **New/un-bootstrapped accounts**: An account can appear in an activity statement with no prior bootstrap. Skip + flag it until BOTH an Unrealized (equity lots + init date) and a Holdings (cash init) intake bootstrap it; never crash, never partially process.

---

## Dependencies

```
# requirements.txt
pandas>=2.0
gspread>=5.0
google-auth-oauthlib>=1.0
google-auth-httplib2>=0.1
google-api-python-client>=2.0
yfinance>=0.2
```

---

## GitHub Repo

- **Repo URL:** `https://github.com/humble-methods/investment-sheet-manager`
- **Primary branch:** `main` (Colab notebook always pulls from `main`)
- **Colab install command:** `pip install git+https://github.com/humble-methods/investment-sheet-manager.git@main`
