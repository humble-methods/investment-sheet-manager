from pathlib import Path

from portfolio.parsers.holdings_parser import parse_holdings_csv

SAMPLE = Path(__file__).parent / "sample_data" / "holdings_sample.csv"


def test_equity_and_cash_maps():
    equity, cash, _ = parse_holdings_csv(SAMPLE)
    assert equity[("11A-00001", "AMD")] == 20.0
    assert equity[("11A-00003", "COF")] == 81.0
    # Cash sweep + Roth money market come ONLY from Holdings.
    assert cash["11A-00001"] == 1500.0
    assert cash["22B-00001"] == 3000.0


def test_cash_rows_excluded_from_equity():
    equity, _, _ = parse_holdings_csv(SAMPLE)
    assert not any(symbol in ("IIAXX", "--") for _, symbol in equity)


def test_registration_map_covers_roth_cash_only_account():
    # Regression: 22B-00001 appears only as IIAXX cash (no equity/INIT_BUY tx),
    # so its registration must come from Holdings — else cash reconciliation
    # mis-labels it as a CMA sweep account.
    _, _, registrations = parse_holdings_csv(SAMPLE)
    assert registrations["22B-00001"] == "Roth IRA-Edge"
    assert registrations["11A-00001"] == "CMA-Edge"
    assert registrations["11A-00003"] == "CMA-Edge"
