"""Parse Merrill Unrealized CSVs into synthetic INIT_BUY Transactions."""

import csv
from pathlib import Path

from portfolio.models import Transaction
from portfolio.parsers.utils import clean_symbol, parse_amount, parse_date, strip_field


def parse_unrealized_csv(filepath: str | Path) -> list[Transaction]:
    """
    Convert each lot row into an INIT_BUY Transaction.

    - Cash rows (CUSIP 990156937 or symbol IIAXX) are skipped.
    - trade_date = settlement_date = Acquisition Date.
    - amount = -(Cost Basis) — money left the account on the original purchase.
    - source_file = "INIT:<filename>".

    Every row is a distinct lot. Identical (account, symbol, date, qty, price)
    lots within a file are real (e.g. AXP 11A-00003 has two 5 sh @ $164.35 on
    11/26/2021) and must NOT be inter-deduped here.
    """
    filepath = Path(filepath)
    transactions: list[Transaction] = []

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            symbol = clean_symbol(row["Symbol"], cusip=row["CUSIP #"])
            if symbol is None:
                continue

            trade_date = parse_date(row["Acquisition Date"])
            cost_basis = parse_amount(row["Cost Basis ($)"]) or 0.0

            transactions.append(Transaction(
                trade_date=trade_date,
                settlement_date=trade_date,
                status="Settled",
                account_number=row["Account #"].strip(),
                account_registration=row["Account Registration"].strip(),
                tx_type="INIT_BUY",
                description=strip_field(row["Security Description"]),
                symbol=symbol,
                quantity=parse_amount(row["Quantity"]),
                price=parse_amount(row["Unit Cost ($)"]),
                amount=-cost_basis,
                source_file=f"INIT:{filepath.name}",
            ))

    return transactions
