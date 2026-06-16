# Local Testing & Validation Plan (agent handoff)

Goal: let a human validate the engine **on real Merrill CSVs, on disk, with no
Drive / Sheets / Colab / network**, by diffing computed output against Merrill's
own ground-truth numbers. Today only the cloud path (`portfolio.runner.run_update`)
can run the full pipeline — it hard-requires Drive creds + 5 env vars + gspread.
This plan adds a local path that reuses the existing pure engine code.

## Context the executing agent must know

- **Interpreter:** repo `.venv` is Python 3.9.6 but the code uses 3.11 syntax
  (`X | None`, etc.). **Run everything with `python3` (3.11), never `.venv/bin/python`.**
  `python3 -m pytest -q` currently → `115 passed`.
- **Real data lives in `sample.local/`** (gitignored via `*.local`). It contains:
  `Unrealized_AllAccounts_022026.csv`, `Holdings_AllAccounts_022026.csv`,
  `Realized_AllAccounts_022026.csv` (all COB **1/30/2026**), and
  `PendingAndSettledActivity_052026_052026.csv` (**05/2026**, a *separate personal*
  account set — only partial overlap with the snapshot accounts).
- **Never commit `sample.local/` or any run output.** Write all output under
  `out.local/` (already gitignored by `*.local`). Real account numbers must not
  enter any tracked file.
- The pure engine functions already exist and are network-free. Confirmed signatures:
  - `parse_unrealized_csv(filepath) -> list[Transaction]`
  - `parse_activity_csv(filepath) -> list[Transaction]`
  - `parse_holdings_csv(filepath) -> tuple[equity{(acct,sym):qty}, cash{acct:$}, registrations{acct:reg}]`
  - `detect_csv_type(filename) -> str`  ("unrealized"|"holdings"|"activity"|"realized"|"unknown")
  - `compute_holdings(transactions, account_state) -> (list[Position], skipped_accounts)`
  - `verify_against_snapshot(positions, snapshot_equity, tolerance=...) -> list[str]`  ("OK ..."/"MISMATCH ...")
  - `reconstruct_cash(transactions, bootstrap_cash, account_state) -> {acct:$}`
  - `reconcile_cash(reconstructed, snapshot_cash, as_of_date, registrations, tolerance=...) -> list[CashBalance]`
  - `runner._cob_date_from_unrealized(path) -> "M/D/YYYY"`  (move this in Task 1)

## What the data can and cannot validate (read before coding)

| Oracle | Source of truth | Runnable now? | Notes |
|--------|-----------------|---------------|-------|
| Holdings quantity | `Holdings_*.csv` `Quantity` | **YES (strong)** | Only in **bootstrap mode**: INIT_BUY-only, no activity, same COB. Must match exact. |
| Cost basis | `Unrealized_*.csv` `Cost Basis` | YES (weak) | Semi-circular (INIT derived from Unrealized) but catches parse/sign/rounding bugs. |
| Cash balance | `Holdings_*.csv` sweep/IIAXX `Value` | YES (bootstrap only) | Mode-1 trivially equal. Real reconcile needs a Holdings CSV dated at activity end. |
| Realized P&L | `Realized_*.csv` `Gain/Loss` | **NO — needs data** | Feb Realized predates the Jan snapshot; pre-snapshot lots absent → cannot replay. Mark deferred. |

Consequence: **bootstrap fidelity is the primary numeric oracle.** Activity replay
(May) has no matching later Holdings snapshot in `sample.local/`, so it is a
**smoke test only** (no oversell crash; un-bootstrapped accounts flagged; changes
directionally sane). State this clearly in the run summary.

---

## Task 1 — Factor pipeline out of the cloud runner (refactor, no behavior change)

Extract the orchestration core (current `run_update` steps 4–18) into a pure,
I/O-free function so both cloud and local paths share one code path (kills
duplication and means the refactor re-validates the cloud path too).

New file `portfolio/pipeline.py`:

```python
@dataclass
class PipelineResult:
    transactions: list[Transaction]      # all parsed (INIT + activity)
    positions: list[Position]
    skipped_accounts: list[str]
    verify_lines: list[str]              # from verify_against_snapshot
    cash_balances: list[CashBalance]
    account_state: dict[str, dict]       # possibly updated with new init dates

def run_pipeline(
    unrealized_paths: list[Path],
    holdings_paths: list[Path],
    activity_paths: list[Path],
    account_state: dict[str, dict],
    *,
    fetch_fund: bool = False,            # local default OFF (yfinance flaky locally)
) -> PipelineResult: ...
```

Rules:
- Move `_cob_date_from_unrealized` from `runner.py` into `pipeline.py`; import back into `runner.py`.
- `run_pipeline` does: parse all files → record new accounts' init_date (COB) into
  `account_state` (first-intake-wins, never overwrite) → `compute_holdings` →
  `verify_against_snapshot` (only if a holdings equity map exists) →
  `reconstruct_cash` + `reconcile_cash`. No Drive, no Sheets, no `print` of secrets.
- Rewrite `run_update` to: do Drive download → call `run_pipeline(...)` →
  do Sheets writes + Drive archive + `save_account_state`. **No logic change.**
- Acceptance: `python3 -m pytest -q` still `115 passed` (or add tests if a gap appears).
  If any cloud-only test breaks, the refactor changed behavior — fix until green.

## Task 2 — Local entrypoint `portfolio/runner_local.py`

CLI:
```
python3 -m portfolio.runner_local <csv_dir> [--out out.local] [--state out.local/account_state.json] [--fundamentals]
```
Behavior:
1. Glob `<csv_dir>/*.csv`; classify each via `detect_csv_type(name)`.
2. Load `account_state` from `--state` JSON if it exists, else `{}` (plain file I/O,
   NOT `archiver.load_account_state` which needs a Drive service).
3. Call `run_pipeline(...)` with `fetch_fund=args.fundamentals` (default False).
4. Write `account_state` back to `--state`.
5. Write outputs under `--out` (create dir): `holdings.csv`, `cash.csv`,
   `transactions.csv`, and the three diff reports below.
6. Print a summary block; **exit code 1 if any diff has a mismatch**, else 0
   (so it is CI/scriptable).

## Task 3 — Diff reports (the human-facing oracle output)

Write under `--out`:

- `holdings_diff.csv` — columns: `account_number, symbol, computed_qty, merrill_qty, diff`.
  Source: `verify_against_snapshot` already produces OK/MISMATCH lines; also emit
  the structured CSV. One row per (account, symbol) in the union. `diff` rounded 4dp.
- `costbasis_diff.csv` — columns: `account_number, symbol, computed_cost, merrill_cost, diff`.
  `computed_cost` = sum of `Position.cost_basis` per (acct, sym). `merrill_cost` =
  sum of Unrealized `Cost Basis` per (acct, sym) (parse separately from the Unrealized file).
- `cash_diff.csv` — from `reconcile_cash`: `account_number, cash_account, reconstructed, snapshot, drift`.

Summary to stdout (plain, no secrets beyond account numbers which stay local):
```
MODE: bootstrap-only | bootstrap+activity   (auto: activity present?)
holdings:  N matched, M MISMATCH
costbasis: N matched, M MISMATCH
cash:      N matched, M drift
skipped (un-bootstrapped): <accounts>
NOTE: activity replay is smoke-test only — no matching post-activity Holdings snapshot.
```

## Task 4 — Smoke test (synthetic, committable)

`tests/test_local_runner.py`: build a 2-row tiny Unrealized + matching Holdings in
a tmp dir (fake tickers/accounts — NOT real data), run `run_pipeline`, assert
`holdings_diff` all-match and exit logic works. Keep it CI-safe (no real CSVs, no network).

---

## The part NO agent does — human outside oracle

Echo-chamber risk: engine, local runner, and agent tests are all Claude-written.
Before reading any agent output, the human hand-picks **one small account** (e.g. the
Roth, or a 2-position CMA account), paper-traces its INIT lots straight from
`Unrealized_*.csv`, and writes the expected per-symbol qty + cost into
`tests/test_oracle_handchecked.py` (kept local / scrubbed). Run bootstrap mode; that
hand-derived number is the only truly independent check.

To upgrade activity replay from smoke-test to numeric oracle: human exports a fresh
**Holdings CSV dated at the activity-period end** (same accounts as the activity file)
and drops it in `sample.local/`. Then computed (INIT + activity) must equal that
Holdings snapshot exactly — closes the loop.

## Run order for the human, once built

```
python3 -m pytest -q                                   # 1. units green (+ smoke)
python3 -m portfolio.runner_local sample.local --out out.local   # 2. real-data diffs
# inspect out.local/holdings_diff.csv — expect ALL match (bootstrap fidelity)
# then re-check against tests/test_oracle_handchecked.py
```
Bootstrap holdings_diff must be all-match. Any MISMATCH = real bug (parse, FIFO,
sign, or dedup) — investigate before trusting activity replay or wiring the cloud path.
