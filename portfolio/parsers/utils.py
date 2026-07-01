"""Shared parsing helpers: numbers, dates, symbols, descriptions."""

import re
from datetime import date, datetime
from pathlib import Path

from portfolio.market.symbol_overrides import is_cash, normalize_symbol

# Merrill Description 2 boilerplate: cut from whichever marker appears first.
DESCRIPTION_MARKERS = ("ACTUAL PRICES, REMUNERATION", "CLIENT ENTERED.")

# Split/stock-dividend rows embed the pre-event share count as "HOLDING N.NNNN"
# in Description 2 (e.g. "...HOLDING 10.0000 PAY DATE..."). Used to cross-check
# the engine's running quantity against the broker at split time (Phase 19).
_HOLDING_RE = re.compile(r"HOLDING\s+([\d,]+(?:\.\d+)?)")


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
    Normalization lives in portfolio.market.symbol_overrides (single source of
    truth); this just adds the parser-side blank/"--" handling.
    """
    raw = raw.strip()
    if raw in ("", "--"):
        return None
    if is_cash(raw, cusip):
        return None
    return normalize_symbol(raw)


def clean_description(raw: str) -> str:
    """
    Strip Merrill boilerplate from Description 2. Cut from the FIRST of
    DESCRIPTION_MARKERS that appears, whichever occurs earliest.
    """
    cut_positions = [pos for marker in DESCRIPTION_MARKERS if (pos := raw.find(marker)) != -1]
    if cut_positions:
        raw = raw[: min(cut_positions)]
    return raw.strip()


def parse_holding_base(description: str) -> float | None:
    """
    Extract the broker's pre-event share count from a "HOLDING N" fragment in a
    Description 2 string, or None if absent. Used to cross-check the engine's
    running quantity when applying a split / stock dividend (Phase 19).
    """
    match = _HOLDING_RE.search(description or "")
    if match is None:
        return None
    return float(match.group(1).replace(",", ""))


def strip_field(value: str) -> str:
    """Trim surrounding whitespace (real exports ship "Purchase ", "Sale ")."""
    return value.strip()


def detect_csv_type(filename: str, filepath: Path | str | None = None) -> str:
    """
    Detect Merrill CSV type by filename first, then by header content as fallback.

    "PendingAndSettledActivity_*" -> "activity"
    "Holdings_*"                  -> "holdings"
    "Realized_*"                  -> "realized"
    "Unrealized_*"                -> "unrealized"
    else -> sniff headers from filepath (if provided), or "unknown"
    """
    import csv as _csv

    name = Path(filename).name
    if name.startswith("PendingAndSettledActivity") or name.startswith("Settled"):
        return "activity"
    if name.startswith("Holdings"):
        return "holdings"
    if name.startswith("Realized"):
        return "realized"
    if name.startswith("Unrealized"):
        return "unrealized"

    if filepath is None:
        return "unknown"

    try:
        with open(filepath, newline="", encoding="utf-8-sig") as fh:
            headers = {h.strip() for h in next(_csv.reader(fh))}
        if "Trade Date" in headers:
            return "activity"
        if "Unit Cost ($)" in headers:
            return "unrealized"
        if "Liquidation Date" in headers:
            return "realized"
        if "Price ($)" in headers:
            return "holdings"
    except Exception:
        pass
    return "unknown"
