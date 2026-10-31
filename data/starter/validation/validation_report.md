# Validation report: starter

**Overall: FAIL.** 31 checks run, 6 with violations, 21 violations on 15 distinct records.

## By category

| Category | Checks | Rows examined | Violations | Status |
|---|---|---|---|---|
| PK_UNIQUENESS | 2 | 194,792 | 0 | PASS |
| FK_INTEGRITY | 2 | 51,787 | 0 | PASS |
| ALLOWED_ENUMS | 1 | 28,819 | 0 | PASS |
| ID_FORMAT | 2 | 39,410 | 0 | PASS |
| DATE_ORDERING | 6 | 35,443 | 3 | FAIL |
| ARITHMETIC_RECONCILIATION | 7 | 28,517 | 12 | FAIL |
| LIFECYCLE_TRANSITIONS | 5 | 7,950 | 0 | PASS |
| DUPLICATES | 4 | 6,450 | 3 | FAIL |
| CURRENCY_CONSISTENCY | 2 | 17,113 | 3 | FAIL |

## Checks

| Check | Category | Rows examined | Violations | What it verifies |
|---|---|---|---|---|
| `PK_UNIQUE` | PK_UNIQUENESS | 30,771 | 0 | Primary key and UNIQUE constraints hold on every table |
| `NOT_NULL` | PK_UNIQUENESS | 164,021 | 0 | Columns declared NOT NULL are never empty, primary key parts included |
| `FK_EXISTS` | FK_INTEGRITY | 50,390 | 0 | Every non-null foreign key points at an existing parent row |
| `FK_SETTLEMENT_XOR` | FK_INTEGRITY | 1,397 | 0 | A settlement link points at exactly one of receivable / payable |
| `ENUM_VALUES` | ALLOWED_ENUMS | 28,819 | 0 | Every constrained column holds only values the schema allows |
| `ID_REGEX` | ID_FORMAT | 32,855 | 0 | Every identifier (and every foreign key to one) matches its documented format |
| `ID_CONSISTENT` | ID_FORMAT | 6,555 | 0 | The parts of an ID agree with the row: entity in account id, year in ledger/document ids, rate id with currency and date |
| `DATE_FORMAT` | DATE_ORDERING | 23,592 | 0 | Every date column holds a real YYYY-MM-DD date |
| `DATE_PAIRS` | DATE_ORDERING | 3,518 | 0 | Within a row: invoice <= due and expected, effective_from <= effective_to, cutoff < horizon end, source data <= cutoff... see detail |
| `DATE_CUTOFF` | DATE_ORDERING | 600 | 3 | A forecast item uses no data dated after its run's cutoff (source data, source ledger entry, FX rate) |
| `DATE_EVENTS` | DATE_ORDERING | 4,881 | 0 | Status events run in date order, start on the document's invoice date and end on a date that matches the stored status |
| `DATE_SETTLEMENT` | DATE_ORDERING | 1,406 | 0 | A ledger entry that settles a document is not dated before the document, and a reversal is not dated before its original |
| `DATE_ACTUALS_AFTER_CUTOFF` | DATE_ORDERING | 1,446 | 0 | A forecast-vs-actual row compares against an actual dated after the run cutoff and not after the compared-through date |
| `POSITION_RECONCILES` | ARITHMETIC_RECONCILIATION | 6,129 | 6 | Daily position: closing = opening + credits - debits; opening = previous closing; closing and totals equal the ledger |
| `FX_COMPLETE` | ARITHMETIC_RECONCILIATION | 9,729 | 3 | A non-base-currency amount carries an FX rate and a base amount; a base-currency amount carries neither a rate nor a conversion |
| `FX_RECOMPUTE` | ARITHMETIC_RECONCILIATION | 7,314 | 3 | Base amount = round(amount x rate x 10^(base exp - local exp)) with the rate the row cites; the rate is for the row's currency and, for actuals, its date |
| `SETTLEMENT_ALLOCATION` | ARITHMETIC_RECONCILIATION | 2,810 | 0 | Allocations of a settlement entry sum to its amount; a document is never allocated more than its amount |
| `PROJECTION_ARITHMETIC` | ARITHMETIC_RECONCILIATION | 1,080 | 0 | Projected closing = opening + inflow - outflow; shortfall flag = (closing < buffer); each run-day follows the previous one |
| `VARIANCE_ARITHMETIC` | ARITHMETIC_RECONCILIATION | 1,446 | 0 | Variance = actual - forecast; amount and FX parts add up to it; date variance = actual date - forecast date |
| `REVERSAL_MIRRORS` | ARITHMETIC_RECONCILIATION | 9 | 0 | A reversal entry has the original's account and amount and the opposite direction |
| `LIFECYCLE_RECEIVABLE` | LIFECYCLE_TRANSITIONS | 1,837 | 0 | Receivable events follow the allowed transitions; the stored status is the last event's status |
| `LIFECYCLE_PAYABLE` | LIFECYCLE_TRANSITIONS | 1,630 | 0 | Payable events follow the allowed transitions; the stored status is the last event's status |
| `LIFECYCLE_RUN` | LIFECYCLE_TRANSITIONS | 30 | 0 | Forecast-run events follow the allowed transitions; stored status is the last event's; SUPERSEDED means a later version exists |
| `LIFECYCLE_ACTIVITY_WINDOW` | LIFECYCLE_TRANSITIONS | 3,000 | 0 | No ledger entry is dated on or after its entity's suspension, or after the closure of its account or entity |
| `LIFECYCLE_SETTLEMENT_STATE` | LIFECYCLE_TRANSITIONS | 1,453 | 0 | A SETTLED document is fully allocated, an OPEN one has no allocation, a PARTIALLY_SETTLED one has some |
| `DUP_FORECAST_ITEM` | DUPLICATES | 600 | 3 | No forecast item repeats another in the same run: all fields equal except the item id |
| `DUP_LEDGER_REF` | DUPLICATES | 3,000 | 0 | Bank reference numbers on ledger entries are unique |
| `DUP_SETTLEMENT_LINK` | DUPLICATES | 1,397 | 0 | The same document is not linked to the same ledger entry twice |
| `DUP_DOCUMENT` | DUPLICATES | 1,453 | 0 | A counterparty does not have two receivables (payables) with the same document reference and amount |
| `CURRENCY_ACCOUNT` | CURRENCY_CONSISTENCY | 11,182 | 3 | A row's currency equals the currency of its bank account |
| `CURRENCY_CODES` | CURRENCY_CONSISTENCY | 5,931 | 0 | Currency codes used anywhere exist in the currency table and rates quote the base currency |

## Violations (first rows of each failing check)

### `DATE_CUTOFF` (3)

| Table | Record | Detail |
|---|---|---|
| cash_flow_forecast | `CF-2026-000099` | source_data_date 2026-02-20, source entry date 2026-02-20, fx date nan, cutoff 2026-01-30 |
| cash_flow_forecast | `CF-2026-000369` | source_data_date 2026-07-13, source entry date 2026-07-13, fx date 2026-06-30, cutoff 2026-06-30 |
| cash_flow_forecast | `CF-2026-000426` | source_data_date 2026-07-09, source entry date 2026-07-09, fx date 2026-06-30, cutoff 2026-06-30 |

### `POSITION_RECONCILES` (6)

| Table | Record | Detail |
|---|---|---|
| daily_cash_position | `BANKACC-ENT-000002-001|2026-12-31` | ARITHMETIC: expected -39357132, found -40573202 |
| daily_cash_position | `BANKACC-ENT-000003-001|2026-10-09` | ARITHMETIC: expected 3862459968, found 3508874438 |
| daily_cash_position | `BANKACC-ENT-000004-002|2026-12-31` | ARITHMETIC: expected 6245206056, found 6841022264 |
| daily_cash_position | `BANKACC-ENT-000002-001|2026-12-31` | LEDGER_CUMULATIVE: expected -39357132, found -40573202 |
| daily_cash_position | `BANKACC-ENT-000003-001|2026-10-09` | LEDGER_CUMULATIVE: expected 3862459968, found 3508874438 |

### `FX_COMPLETE` (3)

| Table | Record | Detail |
|---|---|---|
| ledger_entry | `LED-2026-00001107` | no fx_rate_id on a non-base row |
| ledger_entry | `LED-2026-00002119` | no fx_rate_id on a non-base row |
| ledger_entry | `LED-2026-00002598` | no fx_rate_id on a non-base row |

### `FX_RECOMPUTE` (3)

| Table | Record | Detail |
|---|---|---|
| daily_cash_position | `BANKACC-ENT-000002-001|2026-12-31` | base -44842674 != expected -46228238 |
| daily_cash_position | `BANKACC-ENT-000003-001|2026-10-09` | base 42749977 != expected 38836468 |
| daily_cash_position | `BANKACC-ENT-000004-002|2026-12-31` | base 69488472 != expected 76117934 |

### `DUP_FORECAST_ITEM` (3)

| Table | Record | Detail |
|---|---|---|
| cash_flow_forecast | `CF-2026-000598` | same content as CF-2026-000255 |
| cash_flow_forecast | `CF-2026-000599` | same content as CF-2026-000366 |
| cash_flow_forecast | `CF-2026-000600` | same content as CF-2026-000111 |

### `CURRENCY_ACCOUNT` (3)

| Table | Record | Detail |
|---|---|---|
| ledger_entry | `LED-2026-00001492` | currency USD, account currency GBP |
| ledger_entry | `LED-2026-00001545` | currency USD, account currency GBP |
| ledger_entry | `LED-2026-00001926` | currency USD, account currency GBP |

## Comparison with the labelled anomalies

Labelled cases: 15. Caught by their expected check: 15. Caught by any check: 15. Flagged records that are not labelled: **0**.

| Issue type | Cases | Caught | Expected check |
|---|---|---|---|
| CURRENCY_MIXED_WITHOUT_CONVERSION | 3 | 3 | `CURRENCY_ACCOUNT` |
| DOUBLE_COUNTED_CASH_FLOW | 3 | 3 | `DUP_FORECAST_ITEM` |
| FORECAST_USES_ACTUAL_AFTER_CUTOFF | 3 | 3 | `DATE_CUTOFF` |
| FX_RATE_MISSING | 3 | 3 | `FX_COMPLETE` |
| OPENING_PLUS_LEDGER_NE_CLOSING | 3 | 3 | `POSITION_RECONCILES` |

