"""Core dataclasses: Transaction, Lot, Position, CashBalance, RunLogEntry."""

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Transaction:
    trade_date: date
    settlement_date: date
    status: str             # always "Settled" (Pending rows are skipped)
    account_number: str
    account_registration: str  # "CMA-Edge" | "Roth IRA-Edge"
    tx_type: str            # BUY | SELL | INIT_BUY | SPLIT | DIVIDEND | INTEREST |
                            # CASH_IN | CASH_OUT | CASH_TRANSFER_IN | ADR_FEE |
                            # TAX_WITHHOLDING | REINVEST | CONTRIBUTION_INFO | UNKNOWN
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


@dataclass(frozen=True)
class CorporateAction:
    """A corporate action that remaps an OLD ticker onto a current one.

    A structured record rather than a bare ``old -> new`` string, because a
    corporate action carries more than a target symbol (its kind, context, and —
    for future actions like splits/mergers — ratios or effective dates). Today only
    ``new_symbol`` drives normalization (see ``normalize_symbol``); ``kind`` and
    ``note`` document the event. Extend this dataclass (not the call sites) when
    Phase 19 adds share-changing actions.
    """
    new_symbol: str          # current ticker the old symbol maps to
    kind: str = "rename"     # "rename" | "merger" (split/stock-dividend → Phase 19)
    note: str = ""           # human context (e.g. company names, CUSIP)


@dataclass
class RunLogEntry:
    run_timestamp: str
    files_processed: int
    init_rows_added: int
    transactions_added: int
    accounts_skipped: str    # un-bootstrapped accounts deferred this run
    errors: str
    holdings_changed: str
    cash_reconciliation: str  # per-account reconstructed vs snapshot drift
    duration_sec: float
    notes: str
