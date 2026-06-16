"""Tests for detect_csv_type — filename-based detection and content-based fallback."""

import pytest

from portfolio.parsers.utils import detect_csv_type

SAMPLE_DATA = __import__("pathlib").Path(__file__).parent / "sample_data"


# ---------------------------------------------------------------------------
# Filename-based detection (standard Merrill export names)
# ---------------------------------------------------------------------------


def test_filename_activity_pending_and_settled():
    assert detect_csv_type("PendingAndSettledActivity_012026_032026.csv") == "activity"


def test_filename_activity_settled():
    assert detect_csv_type("SettledActivity_012026_032026.csv") == "activity"


def test_filename_holdings():
    assert detect_csv_type("Holdings_AllAccounts_122025.csv") == "holdings"


def test_filename_realized():
    assert detect_csv_type("Realized_AllAccounts_122025.csv") == "realized"


def test_filename_unrealized():
    assert detect_csv_type("Unrealized_AllAccounts_122025.csv") == "unrealized"


def test_filename_unknown_no_filepath():
    assert detect_csv_type("MyExport.csv") == "unknown"


def test_filename_unknown_with_no_filepath_kwarg():
    assert detect_csv_type("activity_1.csv", None) == "unknown"


# ---------------------------------------------------------------------------
# Content-based fallback (non-standard filenames, filepath provided)
# ---------------------------------------------------------------------------


def test_fallback_activity(tmp_path):
    f = tmp_path / "activity_1.csv"
    f.write_text('"Trade Date","Settlement Date","Pending/Settled","Account #"\n')
    assert detect_csv_type("activity_1.csv", f) == "activity"


def test_fallback_unrealized(tmp_path):
    f = tmp_path / "PersonalAccounts.csv"
    f.write_text('"COB Date","Acquisition Date","Unit Cost ($)","Cost Basis ($)"\n')
    assert detect_csv_type("PersonalAccounts.csv", f) == "unrealized"


def test_fallback_realized(tmp_path):
    f = tmp_path / "realized_export.csv"
    f.write_text('"Acquisition Date","Liquidation Date","Gain/Loss ($)"\n')
    assert detect_csv_type("realized_export.csv", f) == "realized"


def test_fallback_holdings(tmp_path):
    f = tmp_path / "snapshot.csv"
    f.write_text('"COB Date","Symbol","Price ($)","Value ($)"\n')
    assert detect_csv_type("snapshot.csv", f) == "holdings"


def test_fallback_unknown_headers(tmp_path):
    f = tmp_path / "random.csv"
    f.write_text('"foo","bar","baz"\n')
    assert detect_csv_type("random.csv", f) == "unknown"


def test_fallback_empty_file(tmp_path):
    f = tmp_path / "empty.csv"
    f.write_text("")
    assert detect_csv_type("empty.csv", f) == "unknown"


# ---------------------------------------------------------------------------
# Filename takes precedence over content
# ---------------------------------------------------------------------------


def test_filename_wins_over_content(tmp_path):
    # File named like an unrealized export but containing activity headers —
    # filename detection runs first and wins.
    f = tmp_path / "Unrealized_AllAccounts_062026.csv"
    f.write_text('"Trade Date","Settlement Date","Pending/Settled"\n')
    assert detect_csv_type(f.name, f) == "unrealized"


# ---------------------------------------------------------------------------
# Integration: actual sample files with non-standard names
# ---------------------------------------------------------------------------


def test_real_activity_sample_via_fallback():
    # activity_sample.csv has "Trade Date" header; non-standard name triggers fallback.
    path = SAMPLE_DATA / "activity_sample.csv"
    assert detect_csv_type("activity_1.csv", path) == "activity"


def test_real_unrealized_sample_via_fallback():
    path = SAMPLE_DATA / "unrealized_sample.csv"
    assert detect_csv_type("PersonalAccounts.csv", path) == "unrealized"


def test_real_holdings_sample_via_fallback():
    path = SAMPLE_DATA / "holdings_sample.csv"
    assert detect_csv_type("HoldingsSnapshot.csv", path) == "holdings"
