# Liquidity forecasting: synthetic data generator and exception workflow

A deterministic generator of synthetic treasury data (legal entities, bank accounts, ledger, receivables and payables, FX rates, forecast runs under three scenarios), a forecast-versus-actual comparison, five labelled injected data-quality anomalies kept in a separate ground truth, and a detection / validation / evaluation pipeline for them. Built for the YottaFlex AI Round 3 code challenge.

Author: Abhinay Angadi. Repository: https://github.com/AngadiAbhinay01/liquidity-forecast. Submitted 1 November 2026.

AI use: this project was built with Claude (Claude Code), prompt by prompt. See `docs/ai_usage.md` and `prompts/prompt_history.md`. The git history was assembled at the end of the work from that session; the earlier commits carry the AI session's co-author line.

## Problem summary

The workflow consolidates account balances and expected cash flows to forecast short-term liquidity under scenarios. The deliverable is the data that workflow needs, generated reproducibly, plus the evidence that the data is consistent and that its known defects are findable:

* **Entities (9):** LegalEntity, BankAccount, LedgerEntry, CashFlowForecast, Receivable, Payable, CurrencyRate, LiquidityScenario, ForecastRun (plus status-event, settlement-link, daily-position, projection and forecast-vs-actual tables that the workflow needs).
* **Scale:** full = 30 entities, 150 accounts, 100,000 ledger entries, 20,000 forecast items, 5 currencies (USD, EUR, GBP, INR, JPY). Starter = 6 / 18 / 3,000 / 600. Same generator, size set by `--scale`.
* **Fixed seed** 20261022; same command gives byte-identical files (checked across separate processes).
* **Five labelled anomalies**, injected after the valid baseline: opening + ledger != closing, FX rate missing, cash flow counted twice, forecast uses an actual after its cutoff, currency mixed without conversion. Ground truth is written separately and never appears in input tables.
* **Exception path:** detectors that never read the ground truth, an exception queue with severity and reviewer role, an append-only decision log, and precision / recall / F1 per issue type.

## Setup

Python 3.13 (what I used; the pins below were resolved on it).

```bash
python -m venv .venv
source .venv/bin/activate           # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt     # numpy 2.5.3, pandas 3.0.5, pytest 9.1.1 (exact pins)
```

Nothing else is needed: SQLite comes with Python's standard library.

## Run commands

Generate (a command, a seed and an output folder fully determine the files):

```bash
# starter scale (a few seconds)
python -m src.generate --scale starter --seed 20261022 --out data/starter

# full scale (about 30 seconds)
python -m src.generate --scale full --seed 20261022 --out data/full

# valid baseline only (no anomalies; ground truth is then empty)
python -m src.generate --scale starter --no-anomalies --out data/starter_clean
```

`make starter`, `make full`, `make detect`, `make validate`, `make reproduce` run the same commands (`make` targets use `data/starter` and `data/full`).

Detect, evaluate, validate, check reproducibility (replace `data/full` with `data/starter` for the small set):

```bash
python -m src.detect --data data/full                    # exceptions/exception_queue.csv, decision_log.csv
python -m src.detect.evaluate --data data/full           # evaluation/detection_evaluation.{json,md}: P/R/F1 per issue type
python -m src.validate --data data/full --compare-ground-truth
                                                         # validation/validation_summary.json, validation_report.md
python -m src.reproduce --scale full                     # generates twice in separate processes, compares SHA-256 and row counts
```

`src.validate` exits 1 on the shipped data because it contains the labelled anomalies; on a `--no-anomalies` dataset it exits 0.

### What a generated folder contains

| Path | Contents |
|---|---|
| `input/*.csv`, `json/*.json` | 17 tables (the model's input; anomalies are inside them) |
| `sql/schema.sql`, `sql/seed_data.sql`, `db/liquidity.sqlite` | Same data as SQL seed and as a ready SQLite database |
| `derived/` | Consolidated daily position, forecast variance and accuracy reports (computed from the valid baseline) |
| `ground_truth/` | `ground_truth.csv` / `.json`: record id, table, issue type, description, expected decision, reviewer role, injected changes |
| `source_manifest.json` / `.md`, `generator_command.txt`, `run_config.json` | All data synthetic, no external sources, exact command, file sizes and SHA-256 |

Details: `docs/exports_and_reproducibility.md`.

## Tests and validation

```bash
python -m pytest -q                      # about 600 tests, 4 to 20 minutes depending on the machine
python -m pytest tests/test_validation.py -q   # one area at a time
```

The suite covers the schema, ID formats, configuration, each generator stage, anomaly injection (only targeted rows change, no label leakage), detection and workflow, validation (each rule is broken by hand and must fail), exports and cross-process reproducibility. Several tests were checked by mutation (deliberately breaking the code to make sure a test fails); the one known survivor is listed under limitations.

Validation has 31 checks in 9 categories (PK uniqueness, FK integrity, enums, ID format, date ordering, arithmetic reconciliation, lifecycle transitions, duplicates, currency consistency): `docs/validation.md`.

Results from my last run (starter and full): the valid baseline passes every check; the injected dataset is flagged only at the labelled records; detection precision, recall and F1 are 1.000 for every issue type (see the limitation below on how much that proves).

On my own Windows machine (Python 3.13) the full suite gave 599 passed and 2 failed. Both failures were Windows path separators in a test helper (`tests/test_generate_cli.py`), not a data problem; the helper is fixed and that file's 12 tests pass.

## Repository structure

```
src/
  config.py, rng.py, ids.py       scale presets, named random streams from one seed, ID formats
  master/                         entities, bank accounts, FX rates
  txn/                            receivables, payables, ledger, status events, daily positions
  forecast/                       scenarios (four parameters), forecast runs, items, daily projections
  compare/                        forecast vs actual: variance, drivers, MAE / MAPE / WAPE / bias
  anomalies/                      the five-case injector and ground truth
  detect/                         detectors, exception workflow, decision log, evaluation
  validate/                       31 checks, reports, ground-truth comparison, ledger reconciliation
  export.py, manifest.py, reproduce.py, generate.py, pipeline.py, db.py
sql/schema.sql                    17 tables, views, append-only triggers
sql/exceptions.sql                exception queue and decision log (append-only)
docs/                             data dictionary, ER diagram, distributions, design decisions, validation,
                                  exports, review/audit_round1.md
tests/                            pytest suite
```

## Key assumptions and trade-offs

The full list is in `docs/design_decisions.md` (70 numbered decisions). The ones that shape the data most:

* **Money** is integer minor units; amounts are positive with a `direction` column; base currency USD; JPY has no decimals. Base amount = round(amount x rate x 10^(base exponent - local exponent)).
* **Schema enforces structure, not business rules.** Duplicates, cutoff leaks, missing FX and currency mix are views and validators, so injected anomalies can load and be reviewed. Tables that must not change after writing (ledger, forecast items, status events, decision log) have append-only triggers.
* **Scenarios are four integer parameters** (receipt delay days, collection haircut bps, FX shock bps, one-off payment multiplier bps) and nothing else; a test proves two scenarios differ only through them.
* **A forecast only uses data on or before its cutoff**; a re-run of the same cutoff is stored as a new version that supersedes the old one, never an overwrite.
* **Variance drivers are derived from the records** (unforecast, not settled, short payment, timing, amount difference, FX) in a fixed priority order, not assigned at random.
* **Anomalies are injected after the baseline on their own random stream**, change only the targeted rows, and the ground truth lives outside the inputs.
* **Reproducibility** comes from one root seed split into named streams; no clock, host name or global random state; sorted iteration wherever order matters.
* **IDs** follow the problem statement's examples: `BANKACC-<entity-id>-001` with entity id `ENT-000001` gives `BANKACC-ENT-000001-001`; ledger and forecast IDs carry the year (`LED-2026-00000001`, `CF-2026-000001`).
* **Suspended entities** have a `suspended_from` date; nothing is posted for them from that date, balances and open documents stay on the books.

## Known limitations

A strict self-review is in `docs/review/audit_round1.md` (ranked by severity). After it I fixed the forecast row count (the published table now has exactly 20,000 items), suspended entities that kept trading, and the scarcity of large one-off payments (now 180 at full scale). What remains:

* Realism gaps: payroll, tax and debt service have no monthly cycles; no weekends or country holidays (one global business-day calendar); 71 of 150 accounts go overdrawn with no overdraft facility concept.
* Forecast accuracy is modest (item accuracy about 0.55 to 0.58; daily net accuracy negative) and about 70% of forecast items are run-rate blocks. This is reported, not tuned. The `UNFORECAST_ONE_OFF` driver is still rare, because large one-offs are usually visible in advance and so are forecast.
* The forecast run calendar has only 6 cutoff dates, and scenario metrics are scored on different numbers of runs.
* The 1.000 detection scores are partly circular: the anomalies are simple single-field edits and the detectors were written by the same author as the injector.
* One test gap is known (batched documents using the minimum date instead of the batch date).
* The suite takes 4 to 20 minutes depending on the machine and there is no CI configuration.
* With `--no-anomalies` the clean baseline holds the full 20,000 forecast items; with anomalies, 19,990 baseline items plus 10 repeats make 20,000.

## Next steps

1. Work through the rest of the audit (realism and run calendar first).
2. Add monthly payroll / tax / debt-service cycles, country holiday calendars and an overdraft-facility concept.
3. Add harder anomalies (near-duplicates, stale but present FX rates) and score detectors on those.
4. Speed up the suite with shared session fixtures and add CI.
5. Add a thin dashboard over `derived/` and `exceptions/` for reviewers.
