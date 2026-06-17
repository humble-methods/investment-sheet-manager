"""Configuration constants: symbol overrides, Drive/Sheet IDs, account roster."""

import os

# Ticker normalization: Merrill symbol -> yfinance symbol
SYMBOL_OVERRIDES: dict[str, str] = {
    "BRKB": "BRK-B",
}

# Skip these from equity processing (cash/money market)
CASH_CUSIPS: set[str] = {"990156937"}
CASH_SYMBOLS: set[str] = {"IIAXX"}

# Cash / money-market identifiers (per-account cash accounts, ~$1 NAV)
CASH_SWEEP_CUSIP: str = "990156937"  # ML DIRECT DEPOSIT PROGRM (CMA cash)
CASH_MMKT_SYMBOL: str = "IIAXX"      # BofA RASP (Roth cash)

# Google Drive — folder IDs are secrets, NOT committed. Supply at runtime via
# environment (Colab: google.colab.userdata; local: .env / shell env).
# Real values are kept in the untracked .secrets.local.md.
DRIVE_ROOT_FOLDER_ID: str = os.environ.get("DRIVE_ROOT_FOLDER_ID", "")
DRIVE_UPLOAD_FOLDER_ID: str = os.environ.get("DRIVE_UPLOAD_FOLDER_ID", "")
DRIVE_PROCESSED_FOLDER_ID: str = os.environ.get("DRIVE_PROCESSED_FOLDER_ID", "")
DRIVE_FAILED_FOLDER_ID: str = os.environ.get("DRIVE_FAILED_FOLDER_ID", "")
DRIVE_CACHE_FOLDER_ID: str = os.environ.get("DRIVE_CACHE_FOLDER_ID", "")

# Google Sheets — also treat the spreadsheet ID as a secret; supply via env.
SPREADSHEET_ID: str = os.environ.get("SPREADSHEET_ID", "")

# yfinance cache TTL
CACHE_TTL_HOURS: int = 24

# Composition tab: equities whose market weight is below this fraction of their
# scope collapse into a single "Other" slice. Cash is never bucketed.
COMPOSITION_OTHER_THRESHOLD: float = 0.015

# Known account roster (informational; account_number is the canonical key).
# account_registration is NOT unique - multiple accounts share "CMA-Edge".
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
