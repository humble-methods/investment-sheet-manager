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
    # Phase 20: external cash movements. A "Funds Received" wire is the ONLY
    # record of that cash (no matching sweep Deposit row), so it credits cash. A
    # "Current Year Contribution" is recorded for the paper trail only — the same
    # money already lands as an IIAXX Deposit, so counting it too double-counts;
    # reconstruct_cash excludes CONTRIBUTION_INFO from the balance.
    ("FundTransfers", "Funds Received"): "CASH_IN",
    ("FundReceipts", "Current Year Contribution"): "CONTRIBUTION_INFO",
}

# Combos that can carry a share-changing corporate action (split / stock
# dividend). They surface as $0-amount rows with a signed share quantity — the
# "Dividend" combo is otherwise a normal cash dividend, so it is only a SPLIT
# when it moves shares for no money (see below). Phase 19 / Non-Obvious #26.
SPLIT_COMBOS: set[tuple[str, str]] = {
    ("SecurityTransactions", "Stock Dividend Due Bill"),
    ("SecurityTransactions", "Dividend"),
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
            combo = (type_key, desc1_key)

            quantity = parse_amount(row["Quantity"])
            amount = parse_amount(row["Amount ($)"]) or 0.0

            # A split / stock dividend moves shares for no money. Disambiguate it
            # from a normal cash dividend (nonzero amount, no share quantity) on
            # amount == 0 and a present share quantity; only then is it a SPLIT.
            if combo in SPLIT_COMBOS and amount == 0.0 and quantity is not None:
                tx_type = "SPLIT"
            else:
                tx_type = TX_TYPE_MAP.get(combo)
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
                quantity=quantity,
                price=parse_amount(row["Price ($)"]),
                amount=amount,
                source_file=filepath.name,
            ))

    return transactions
