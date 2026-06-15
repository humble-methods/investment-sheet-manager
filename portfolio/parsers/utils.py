"""Shared parsing helpers: numbers, dates, symbols, descriptions."""

from datetime import date, datetime
from pathlib import Path

from portfolio.config import CASH_CUSIPS, CASH_SYMBOLS, SYMBOL_OVERRIDES

# Merrill Description 2 boilerplate: cut from whichever marker appears first.
DESCRIPTION_MARKERS = ("ACTUAL PRICES, REMUNERATION", "CLIENT ENTERED.")


def parse_amount(value: str) -> float | None:
    """
    ""  or "--"     -> None
    "(3,211.38)"    -> -3211.38
    "3,211.38"      -> 3211.38
    "128.46"        -> 128.46
    "19"            -> 19.0
    """
    value = value.strip()
    if value in ("", "--"):
        return None

    negative = value.startswith("(") and value.endswith(")")
    if negative:
        value = value[1:-1]

    value = value.replace(",", "")
    amount = float(value)
    return -amount if negative else amount


def parse_date(value: str) -> date:
    """M/D/YYYY -> date"""
    return datetime.strptime(value.strip(), "%m/%d/%Y").date()


def clean_symbol(raw: str, cusip: str = "") -> str | None:
    """
    Returns normalized yfinance-compatible symbol, or None for cash positions.
    Applies SYMBOL_OVERRIDES. Returns None if cusip in CASH_CUSIPS or raw in
    CASH_SYMBOLS/CASH_CUSIPS.
    """
    raw = raw.strip()
    cusip = cusip.strip()

    if raw in ("", "--"):
        return None
    if raw in CASH_SYMBOLS or raw in CASH_CUSIPS or cusip in CASH_CUSIPS:
        return None

    return SYMBOL_OVERRIDES.get(raw, raw)


def clean_description(raw: str) -> str:
    """
    Strip Merrill boilerplate from Description 2. Cut from the FIRST of
    DESCRIPTION_MARKERS that appears, whichever occurs earliest.
    """
    cut_positions = [pos for marker in DESCRIPTION_MARKERS if (pos := raw.find(marker)) != -1]
    if cut_positions:
        raw = raw[: min(cut_positions)]
    return raw.strip()


def strip_field(value: str) -> str:
    """Trim surrounding whitespace (real exports ship "Purchase ", "Sale ")."""
    return value.strip()


def detect_csv_type(filename: str) -> str:
    """
    "PendingAndSettledActivity_*" -> "activity"
    "Holdings_*"                  -> "holdings"
    "Realized_*"                  -> "realized"
    "Unrealized_*"                -> "unrealized"
    else                          -> "unknown"
    """
    name = Path(filename).name
    if name.startswith("PendingAndSettledActivity"):
        return "activity"
    if name.startswith("Holdings"):
        return "holdings"
    if name.startswith("Realized"):
        return "realized"
    if name.startswith("Unrealized"):
        return "unrealized"
    return "unknown"
