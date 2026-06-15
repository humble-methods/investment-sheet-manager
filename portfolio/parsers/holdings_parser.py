"""Parse Merrill Holdings CSVs into equity verification + cash bootstrap maps."""

import csv
from pathlib import Path

from portfolio.parsers.utils import clean_symbol, parse_amount


def parse_holdings_csv(
    filepath: str | Path,
) -> tuple[dict[tuple[str, str], float], dict[str, float], dict[str, str]]:
    """
    Returns:
        equity:        {(account_number, symbol): quantity} — skips cash rows.
        cash:          {account_number: dollar balance} for the cash sweep
                       (990156937) or Roth money market (IIAXX), which appear
                       ONLY in Holdings (not Unrealized).
        registrations: {account_number: account_registration} for EVERY account
                       seen (equity + cash). This is the authoritative per-account
                       registration source: a Roth held only as IIAXX cash has no
                       equity/INIT_BUY transaction, so it would otherwise be
                       invisible to a transaction-derived map (and mis-labeled as
                       a CMA sweep account during cash reconciliation).
    """
    equity: dict[tuple[str, str], float] = {}
    cash: dict[str, float] = {}
    registrations: dict[str, str] = {}

    with open(Path(filepath), newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            account = row["Account #"].strip()
            registrations[account] = row["Account Registration"].strip()
            quantity = parse_amount(row["Quantity"]) or 0.0
            symbol = clean_symbol(row["Symbol"], cusip=row["CUSIP #"])

            if symbol is None:
                cash[account] = quantity
                continue

            equity[(account, symbol)] = quantity

    return equity, cash, registrations
