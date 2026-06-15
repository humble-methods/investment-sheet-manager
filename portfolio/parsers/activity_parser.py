"""Parse Merrill PendingAndSettledActivity CSVs into Transactions."""

import csv
import logging
from pathlib import Path

from portfolio.models import Transaction
from portfolio.parsers.utils import (
    clean_description,
    clean_symbol,
    parse_amount,
    parse_date,
    strip_field,
)

logger = logging.getLogger(__name__)

TX_TYPE_MAP: dict[tuple[str, str], str] = {
    ("SecurityTransactions", "Purchase"): "BUY",
    ("SecurityTransactions", "Sale"): "SELL",
    ("SecurityTransactions", "Interest"): "REINVEST",
    ("DividendAndInterest", "Dividend"): "DIVIDEND",
    ("DividendAndInterest", "Foreign Dividend"): "DIVIDEND",
    ("DividendAndInterest", "Bank Interest"): "INTEREST",
    ("Other", "Deposit"): "CASH_IN",
    ("Other", "Withdrawal"): "CASH_OUT",
    ("Other", "Depository Bank (ADR) Fee"): "ADR_FEE",
    ("Other", "Foreign Tax Withholding"): "TAX_WITHHOLDING",
}


def parse_activity_csv(filepath: str | Path) -> list[Transaction]:
    """
    Parse a PendingAndSettledActivity CSV into Transactions.

    - Skips rows where Pending/Settled == "Pending".
    - strip_field()s Type / Description 1 before TX_TYPE_MAP lookup (trailing
      spaces ship in real exports).
    - Cash sweep (990156937) / Roth money market (IIAXX) rows are kept as
      transactions with symbol=None (no equity lots created for them).
    - Unmapped (Type, Description 1) combos become tx_type="UNKNOWN" and are
      logged, not raised.
    """
    filepath = Path(filepath)
    transactions: list[Transaction] = []

    with open(filepath, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if strip_field(row["Pending/Settled"]) != "Settled":
                continue

            type_key = strip_field(row["Type"])
            desc1_key = strip_field(row["Description 1 "])
            tx_type = TX_TYPE_MAP.get((type_key, desc1_key))
            if tx_type is None:
                logger.warning(
                    "Unknown (Type, Description 1) combo (%r, %r) in %s",
                    type_key, desc1_key, filepath.name,
                )
                tx_type = "UNKNOWN"

            transactions.append(Transaction(
                trade_date=parse_date(row["Trade Date"]),
                settlement_date=parse_date(row["Settlement Date"]),
                status="Settled",
                account_number=row["Account #"].strip(),
                account_registration=row["Account Registration"].strip(),
                tx_type=tx_type,
                description=clean_description(row["Description 2"]),
                symbol=clean_symbol(row["Symbol/CUSIP #"]),
                quantity=parse_amount(row["Quantity"]),
                price=parse_amount(row["Price ($)"]),
                amount=parse_amount(row["Amount ($)"]) or 0.0,
                source_file=filepath.name,
            ))

    return transactions
