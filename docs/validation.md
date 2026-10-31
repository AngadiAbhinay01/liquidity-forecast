# Validation

`python -m src.validate --data data/ [--compare-ground-truth]` reads `input/*.csv`, runs 31 checks and writes `validation/validation_summary.json`, `validation_report.md` and `validation_violations.csv`. It exits 0 when every check passes and 1 when any record is flagged: the shipped dataset contains labelled anomalies, so exit 1 is its expected result; `--no-anomalies` data exits 0.

Where the rules come from:

* **The schema file.** Primary keys, UNIQUE constraints, foreign keys, NOT NULL and enum lists are read from `sql/schema.sql` (through SQLite's catalogue and a regex over the CHECK clauses) and applied to the DataFrames. A table that could never be loaded into the database is still reported, instead of failing in a loader.
* **`src/ids.py`.** The ID formats, applied to every identifier and every foreign key that holds one, plus checks that the parts of an ID (entity in an account id, year in a ledger or document id, run id from cutoff and sequence, rate id from currency and date) agree with the row.
* **Business rules** in `src/validate/checks.py`. These are not schema constraints, because an anomaly has to be loadable to be reviewed; the same invariants exist as SQL views (`v_*` in the schema), written separately from the pandas code.

The lifecycle transition tables in the validator are a deliberate second copy of the ones the generator uses; a test fails if they diverge.

A check that raises an exception is reported as a violation on the pseudo-table `(check)`; it never counts as a pass. A duplicated key does not crash other checks (lookups keep the first row, and `PK_UNIQUE` reports the duplicate).

## Checks

| Check | Category | What it verifies |
|---|---|---|
| `PK_UNIQUE` | PK_UNIQUENESS | Primary key and UNIQUE constraints hold on every table |
| `NOT_NULL` | PK_UNIQUENESS | Columns declared NOT NULL are never empty, primary key parts included |
| `FK_EXISTS` | FK_INTEGRITY | Every non-null foreign key points at an existing parent row |
| `FK_SETTLEMENT_XOR` | FK_INTEGRITY | A settlement link points at exactly one of receivable / payable |
| `ENUM_VALUES` | ALLOWED_ENUMS | Every constrained column holds only values the schema allows |
| `ID_REGEX` | ID_FORMAT | Every identifier (and every foreign key to one) matches its documented format |
| `ID_CONSISTENT` | ID_FORMAT | The parts of an ID agree with the row: entity in account id, year in ledger/document ids, rate id with currency and date |
| `DATE_FORMAT` | DATE_ORDERING | Every date column holds a real YYYY-MM-DD date |
| `DATE_PAIRS` | DATE_ORDERING | Within a row: invoice <= due and expected, effective_from <= effective_to, cutoff < horizon end, source data <= cutoff... see detail |
| `DATE_CUTOFF` | DATE_ORDERING | A forecast item uses no data dated after its run's cutoff (source data, source ledger entry, FX rate) |
| `DATE_EVENTS` | DATE_ORDERING | Status events run in date order, start on the document's invoice date and end on a date that matches the stored status |
| `DATE_SETTLEMENT` | DATE_ORDERING | A ledger entry that settles a document is not dated before the document, and a reversal is not dated before its original |
| `DATE_ACTUALS_AFTER_CUTOFF` | DATE_ORDERING | A forecast-vs-actual row compares against an actual dated after the run cutoff and not after the compared-through date |
| `POSITION_RECONCILES` | ARITHMETIC_RECONCILIATION | Daily position: closing = opening + credits - debits; opening = previous closing; closing and totals equal the ledger |
| `FX_COMPLETE` | ARITHMETIC_RECONCILIATION | A non-base-currency amount carries an FX rate and a base amount; a base-currency amount carries neither a rate nor a conversion |
| `FX_RECOMPUTE` | ARITHMETIC_RECONCILIATION | Base amount = round(amount x rate x 10^(base exp - local exp)) with the rate the row cites; the rate is for the row's currency and, for actuals, its date |
| `SETTLEMENT_ALLOCATION` | ARITHMETIC_RECONCILIATION | Allocations of a settlement entry sum to its amount; a document is never allocated more than its amount |
| `PROJECTION_ARITHMETIC` | ARITHMETIC_RECONCILIATION | Projected closing = opening + inflow - outflow; shortfall flag = (closing < buffer); each run-day follows the previous one |
| `VARIANCE_ARITHMETIC` | ARITHMETIC_RECONCILIATION | Variance = actual - forecast; amount and FX parts add up to it; date variance = actual date - forecast date |
| `REVERSAL_MIRRORS` | ARITHMETIC_RECONCILIATION | A reversal entry has the original's account and amount and the opposite direction |
| `LIFECYCLE_RECEIVABLE` | LIFECYCLE_TRANSITIONS | Receivable events follow the allowed transitions; the stored status is the last event's status |
| `LIFECYCLE_PAYABLE` | LIFECYCLE_TRANSITIONS | Payable events follow the allowed transitions; the stored status is the last event's status |
| `LIFECYCLE_RUN` | LIFECYCLE_TRANSITIONS | Forecast-run events follow the allowed transitions; stored status is the last event's; SUPERSEDED means a later version exists |
| `LIFECYCLE_ACTIVITY_WINDOW` | LIFECYCLE_TRANSITIONS | No ledger entry is dated on or after its entity's suspension, or after the closure of its account or entity |
| `LIFECYCLE_SETTLEMENT_STATE` | LIFECYCLE_TRANSITIONS | A SETTLED document is fully allocated, an OPEN one has no allocation, a PARTIALLY_SETTLED one has some |
| `DUP_FORECAST_ITEM` | DUPLICATES | No forecast item repeats another in the same run: all fields equal except the item id |
| `DUP_LEDGER_REF` | DUPLICATES | Bank reference numbers on ledger entries are unique |
| `DUP_SETTLEMENT_LINK` | DUPLICATES | The same document is not linked to the same ledger entry twice |
| `DUP_DOCUMENT` | DUPLICATES | A counterparty does not have two receivables (payables) with the same document reference and amount |
| `CURRENCY_ACCOUNT` | CURRENCY_CONSISTENCY | A row's currency equals the currency of its bank account |
| `CURRENCY_CODES` | CURRENCY_CONSISTENCY | Currency codes used anywhere exist in the currency table and rates quote the base currency |

## Reading the result

* **Baseline** (`generate_all`, or `--no-anomalies`): 0 violations on all checks.
* **Injected data:** each labelled anomaly is caught by the check named below, and no other record is flagged. `FX_RECOMPUTE` also fires on a position row whose closing balance was altered (its base value no longer matches), which is the same record.

| Issue type | Check designed to catch it |
|---|---|
| `OPENING_PLUS_LEDGER_NE_CLOSING` | `POSITION_RECONCILES` |
| `FX_RATE_MISSING` | `FX_COMPLETE` |
| `DOUBLE_COUNTED_CASH_FLOW` | `DUP_FORECAST_ITEM` |
| `FORECAST_USES_ACTUAL_AFTER_CUTOFF` | `DATE_CUTOFF` |
| `CURRENCY_MIXED_WITHOUT_CONVERSION` | `CURRENCY_ACCOUNT` |

`--compare-ground-truth` opens `ground_truth/ground_truth.csv` after the checks have run (module `src/validate/labels.py`) and adds a section that reports, per issue type, how many labelled cases tripped their check and how many flagged records are *not* labelled. The checks themselves never see the labels.

## Not checked (on purpose)

* `forecast_daily_projection` and `forecast_vs_actual` are derived tables and are checked for internal arithmetic only. They are not recomputed from `cash_flow_forecast`, so the injected duplicate item (which they do not include) is not reported there.
* Settlement links are not compared with the currency of the document they pay, so one relabelled ledger entry produces one flagged record (the entry), not an extra one on its link.
* Forecast base amounts are not recomputed from rates: scenario FX shocks adjust the rate, so a recomputation needs the scenario parameters.
