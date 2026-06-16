"""Main orchestrator — run_update() is the single entry point called from Colab or CLI."""

from __future__ import annotations

import csv
import logging
import os
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from portfolio.drive.archiver import (
    build_drive_service,
    download_csv,
    list_pending_csvs,
    load_account_state,
    load_yfinance_cache,
    move_to_failed,
    move_to_processed,
    save_account_state,
    save_yfinance_cache,
)
from portfolio.engine.cash import reconcile_cash, reconstruct_cash
from portfolio.engine.holdings import compute_holdings, verify_against_snapshot
from portfolio.market.yfinance_client import fetch_fundamentals
from portfolio.models import RunLogEntry, Transaction
from portfolio.parsers.activity_parser import parse_activity_csv
from portfolio.parsers.holdings_parser import parse_holdings_csv
from portfolio.parsers.unrealized_parser import parse_unrealized_csv
from portfolio.parsers.utils import detect_csv_type
from portfolio.sheets.writer import (
    get_gspread_client,
    load_existing_source_files,
    load_existing_transaction_keys,
    write_cash,
    write_holdings,
    write_run_log,
    write_stock_metrics,
    write_transactions,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_REQUIRED_ENV = [
    "DRIVE_UPLOAD_FOLDER_ID",
    "DRIVE_PROCESSED_FOLDER_ID",
    "DRIVE_FAILED_FOLDER_ID",
    "DRIVE_CACHE_FOLDER_ID",
    "SPREADSHEET_ID",
]


def run_update(credentials=None) -> None:
    """Full end-to-end portfolio update. Call from Colab (pass credentials) or CLI."""
    start_time = time.time()
    errors: list[str] = []
    notes: list[str] = []

    # Read config from env at call time — safe for Colab where env vars are set
    # in a cell before this function is called (import time would miss them).
    env = {k: os.environ.get(k, "") for k in _REQUIRED_ENV}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise EnvironmentError(
            f"Set these env vars before running: {', '.join(missing)}"
        )
    upload_id    = env["DRIVE_UPLOAD_FOLDER_ID"]
    processed_id = env["DRIVE_PROCESSED_FOLDER_ID"]
    failed_id    = env["DRIVE_FAILED_FOLDER_ID"]
    cache_id     = env["DRIVE_CACHE_FOLDER_ID"]
    sheet_id     = env["SPREADSHEET_ID"]

    # --- 1. Drive service ---
    service = build_drive_service(credentials)

    # --- 2. Load account state (per-account init dates + bootstrap source files) ---
    account_state: dict[str, dict] = load_account_state(service, cache_id)
    print(f"Account state: {len(account_state)} bootstrapped account(s)")

    # --- 3. List + download CSVs from upload folder ---
    pending = list_pending_csvs(service, upload_id)
    if not pending:
        print("No CSV files found in upload folder — nothing to do.")
        return
    print(f"Found {len(pending)} CSV file(s) in upload folder")

    tmpdir = Path(tempfile.mkdtemp(prefix="portfolio_"))
    downloaded: list[dict] = []  # {id, name, path, type, failed}

    for f in pending:
        dest = tmpdir / f["name"]
        try:
            download_csv(service, f["id"], dest)
            downloaded.append({
                **f,
                "path": dest,
                "type": detect_csv_type(f["name"]),
                "failed": False,
            })
        except Exception as exc:
            msg = f"Download failed — {f['name']}: {exc}"
            logger.error(msg)
            errors.append(msg)
            downloaded.append({**f, "path": None, "type": "unknown", "failed": True})

    # --- 4. Partition by CSV type ---
    unrealized_files = [d for d in downloaded if not d["failed"] and d["type"] == "unrealized"]
    holdings_files   = [d for d in downloaded if not d["failed"] and d["type"] == "holdings"]
    activity_files   = [d for d in downloaded if not d["failed"] and d["type"] == "activity"]
    unknown_files    = [d for d in downloaded if not d["failed"] and d["type"] not in
                        {"unrealized", "holdings", "activity"}]

    for d in unknown_files:
        notes.append(f"Skipped unrecognized file: {d['name']} (type={d['type']})")

    # --- 5. Parse unrealized → INIT_BUY; bootstrap new accounts in account_state ---
    all_transactions: list[Transaction] = []

    for d in unrealized_files:
        try:
            txns = parse_unrealized_csv(d["path"])
            cob = _cob_date_from_unrealized(d["path"])
            for tx in txns:
                acc = tx.account_number
                if acc not in account_state and cob:
                    account_state[acc] = {
                        "init_date": cob,
                        "bootstrap_source_file": d["name"],
                    }
                    print(f"  Bootstrapped new account: {acc} (init_date={cob})")
            all_transactions.extend(txns)
        except Exception as exc:
            msg = f"Parse failed — {d['name']}: {exc}"
            logger.error(msg)
            errors.append(msg)
            d["failed"] = True

    # --- 6. Parse holdings → equity verification map + cash bootstrap + registrations ---
    equity_snapshot: dict[tuple[str, str], float] = {}
    bootstrap_cash: dict[str, float] = {}
    account_registrations: dict[str, str] = {}

    for d in holdings_files:
        try:
            equity, cash, regs = parse_holdings_csv(d["path"])
            equity_snapshot.update(equity)
            bootstrap_cash.update(cash)
            account_registrations.update(regs)
        except Exception as exc:
            msg = f"Parse failed — {d['name']}: {exc}"
            logger.error(msg)
            errors.append(msg)
            d["failed"] = True

    # --- 7. Parse activity files (settled only); detect within-file collisions ---
    for d in activity_files:
        try:
            txns = parse_activity_csv(d["path"])
            for warning in _detect_collisions(txns):
                notes.append(f"[{d['name']}] {warning}")
            all_transactions.extend(txns)
        except Exception as exc:
            msg = f"Parse failed — {d['name']}: {exc}"
            logger.error(msg)
            errors.append(msg)
            d["failed"] = True

    # Supplement account_registrations from transaction data (for accounts with no
    # Holdings CSV this run — transactions carry the registration string too).
    for tx in all_transactions:
        if tx.account_number not in account_registrations and tx.account_registration:
            account_registrations[tx.account_number] = tx.account_registration

    # --- 8. Connect to Sheets; load existing dedup state ---
    gc = get_gspread_client(credentials)
    sh = gc.open_by_key(sheet_id)
    existing_keys = load_existing_transaction_keys(sh)
    existing_source_files = load_existing_source_files(sh)
    print(f"Sheets: {len(existing_keys)} existing transaction key(s)")

    # --- 9. Filter INIT_BUY by source_file; activity by per-row dedup key ---
    # INIT_BUY: the entire Unrealized file is the atomic unit — skip if already imported.
    # Do NOT key-dedup INIT rows: identical lots (e.g. AXP: two 5 sh @ same price) are real.
    init_buy_txns = [
        tx for tx in all_transactions
        if tx.tx_type == "INIT_BUY" and tx.source_file not in existing_source_files
    ]
    activity_txns = [tx for tx in all_transactions if tx.tx_type != "INIT_BUY"]

    # --- 10. Write transactions ---
    # Pass empty set for INIT_BUY so identical lots are both persisted.
    init_rows_added = write_transactions(sh, init_buy_txns, set())
    activity_rows_added = write_transactions(sh, activity_txns, existing_keys)
    print(f"Wrote {init_rows_added} INIT_BUY row(s) and {activity_rows_added} activity row(s)")

    # --- 11. Compute holdings (replay full transaction set) ---
    positions, skipped_accounts = compute_holdings(all_transactions, account_state)
    if skipped_accounts:
        for acc in skipped_accounts:
            notes.append(f"Skipped un-bootstrapped account: {acc}")
        print(f"Skipped accounts (no bootstrap): {', '.join(skipped_accounts)}")
    print(f"Holdings: {len(positions)} position(s)")

    # --- 12. Verify vs Holdings snapshot ---
    diffs = verify_against_snapshot(positions, equity_snapshot)
    mismatches = [line for line in diffs if line.startswith("MISMATCH")]
    for m in mismatches:
        logger.warning(m)
    holdings_changed = (
        f"{len(positions)} positions; {len(mismatches)} snapshot mismatch(es)"
        if equity_snapshot else f"{len(positions)} positions; no Holdings CSV this run"
    )

    # --- 13. Write Holdings ---
    write_holdings(sh, positions)

    # --- 14. Reconstruct + reconcile cash ---
    reconstructed = reconstruct_cash(all_transactions, bootstrap_cash, account_state)
    snapshot_cash = bootstrap_cash if holdings_files else None
    cash_balances = reconcile_cash(
        reconstructed, snapshot_cash, date.today(), account_registrations
    )
    drifting = [b for b in cash_balances if b.drift is not None and abs(b.drift) > 0.01]
    cash_reconciliation = (
        "; ".join(f"{b.account_number} drift={b.drift:+.2f}" for b in drifting) or "OK"
    )

    # --- 15. Write Cash ---
    write_cash(sh, cash_balances)

    # --- 16. Collect held symbols ---
    symbols = sorted({p.symbol for p in positions if p.symbol})

    # --- 17. Load cache, fetch yfinance fundamentals, save cache ---
    yf_cache = load_yfinance_cache(service, cache_id)
    fundamentals = fetch_fundamentals(symbols, yf_cache)
    save_yfinance_cache(service, cache_id, yf_cache)
    print(f"Fetched fundamentals for {len(fundamentals)} symbol(s)")

    # --- 18. Write Stock Metrics ---
    write_stock_metrics(sh, fundamentals, date.today())

    # --- 19. Save updated account_state ---
    save_account_state(service, cache_id, account_state)

    # --- 20. Archive CSVs ---
    for d in downloaded:
        try:
            if d["failed"]:
                move_to_failed(service, d["id"], d["name"], upload_id, failed_id)
            else:
                move_to_processed(service, d["id"], d["name"], upload_id, processed_id)
        except Exception as exc:
            msg = f"Archive failed — {d['name']}: {exc}"
            logger.error(msg)
            errors.append(msg)

    # --- 21. Write Run Log entry ---
    duration = round(time.time() - start_time, 1)
    entry = RunLogEntry(
        run_timestamp=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        files_processed=len(downloaded),
        init_rows_added=init_rows_added,
        transactions_added=activity_rows_added,
        accounts_skipped=", ".join(skipped_accounts),
        errors="; ".join(errors),
        holdings_changed=holdings_changed,
        cash_reconciliation=cash_reconciliation,
        duration_sec=duration,
        notes="; ".join(notes),
    )
    write_run_log(sh, entry)

    # --- 22. Summary ---
    print("\n=== Run complete ===")
    print(f"  Files processed : {len(downloaded)}")
    print(f"  INIT_BUY rows   : {init_rows_added}")
    print(f"  Activity rows   : {activity_rows_added}")
    print(f"  Positions       : {len(positions)}")
    print(f"  Symbols fetched : {len(fundamentals)}")
    print(f"  Accounts skipped: {', '.join(skipped_accounts) or 'none'}")
    print(f"  Cash drift      : {cash_reconciliation}")
    if errors:
        print(f"  Errors ({len(errors)}):")
        for e in errors:
            print(f"    - {e}")
    print(f"  Duration        : {duration}s")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cob_date_from_unrealized(filepath: Path) -> str:
    """Return the COB Date from the first data row of an Unrealized CSV, or ''."""
    with open(filepath, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            return row.get("COB Date", "").strip()
    return ""


def _detect_collisions(txns: list[Transaction]) -> list[str]:
    """Return a warning string for each dedup_key that appears more than once
    within a single file's batch (platform-split fills need human review)."""
    seen: defaultdict[tuple, int] = defaultdict(int)
    for tx in txns:
        seen[tx.dedup_key] += 1
    return [
        f"IDENTICAL_FILL ({count}x) key={key} — flagged for human review"
        for key, count in seen.items()
        if count > 1
    ]
