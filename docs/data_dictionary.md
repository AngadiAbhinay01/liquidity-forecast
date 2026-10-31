# Data dictionary

Source of truth for structure is `sql/schema.sql`. `tests/test_schema.py` fails if any table or column there is missing from this file.

## Conventions

| Topic | Rule |
|---|---|
| Money | INTEGER minor units. Exponent per currency in `currency.minor_unit_exponent` (USD/EUR/GBP/INR = 2, JPY = 0). |
| Dates | ISO `YYYY-MM-DD` TEXT. Simulated business dates only; no wall-clock timestamps. |
| FX rate | Base-currency units per 1 unit of quote currency. Base is USD. Rates are rounded to 8 decimals before writing. |
| Base amount | `round_half_even(amount_minor * rate * 10^(exp_base - exp_quote))`, computed once at generation and stored. |
| Direction | Amounts are always positive; `direction` (`CREDIT` = money in, `DEBIT` = money out) carries the sign. |
| Req | `Y` = NOT NULL. `N` = nullable (rule given in the validation column). |
| Enforced | **Schema** = CHECK/FK/UNIQUE in `schema.sql`. **Validator** = cross-row or cross-table rule checked by the views and `src/validate`, deliberately not a constraint so labelled anomalies can load. |

### ID formats

| Entity | Format | Example | Notes |
|---|---|---|---|
| LegalEntity | `ENT-nnnnnn` | `ENT-000001` | Sequential, assigned in generation order. |
| BankAccount | `BANKACC-<entity_id>-nnn` | `BANKACC-ENT-000001-001` | Literal reading of the brief: `<entity-id>` is the whole `ENT-000001`. Embedded entity and sequence are CHECKed against the columns. |
| LedgerEntry | `LED-yyyy-nnnnnnnn` | `LED-2026-00000001` | `yyyy` = year of `entry_date`. Counter restarts each year, assigned after sorting by (entry_date, bank_account_id, tie-break key). |
| CashFlowForecast | `CF-yyyy-nnnnnn` | `CF-2026-000001` | `yyyy` = year of the run's cutoff date. Counter restarts each year. |
| ForecastRun | `FCAST-yyyy-mm-dd-nnn` | `FCAST-2026-01-01-001` | Date = cutoff date. `nnn` = `run_seq` within that date. CHECKed against `cutoff_date` and `run_seq`. |
| Receivable | `RCV-yyyy-nnnnnn` | `RCV-2026-000001` | Extension. `yyyy` = invoice year. |
| Payable | `PAY-yyyy-nnnnnn` | `PAY-2026-000001` | Extension. `yyyy` = invoice year. |
| CurrencyRate | `FXR-CCC-yyyymmdd` | `FXR-EUR-20260105` | Extension. Quote currency + date. |
| LiquidityScenario | `SCN-nnn` | `SCN-001` | Extension. |
| ForecastVsActual | `VAR-yyyy-nnnnnn` | `VAR-2026-000001` | Extension. |
| Event, bridge rows | integer surrogate | `1, 2, 3 …` | Assigned in sorted insert order, so deterministic. |

---

## currency  (lookup, supports the 5-currency requirement)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `currency_code` | TEXT | Y | ISO 4217, 3 uppercase letters. Baseline: USD, EUR, GBP, INR, JPY | | PK | Matches `[A-Z]{3}` | Schema |
| `currency_name` | TEXT | Y | Free text | | | | |
| `minor_unit_exponent` | INTEGER | Y | 0, 2, 3 | decimal places | | JPY = 0, others = 2 | Schema |
| `is_base` | INTEGER | Y | 0, 1 | | Exactly one row = 1 | Partial unique index | Schema |

## legal_entity  (LegalEntity, master)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `entity_id` | TEXT | Y | `ENT-nnnnnn` | | PK | GLOB format | Schema |
| `legal_name` | TEXT | Y | Synthetic company name | | Y | Must not resemble a real company | Schema (unique) |
| `country_code` | TEXT | Y | ISO 3166 alpha-2 | | | `[A-Z]{2}` | Schema |
| `functional_currency` | TEXT | Y | FK `currency.currency_code` | | | | Schema |
| `entity_type` | TEXT | Y | `OPERATING`, `HOLDING`, `TREASURY_CENTRE` | | | | Schema |
| `parent_entity_id` | TEXT | N | FK `legal_entity.entity_id`. NULL = top of group | | | Not self; no cycles | Schema (not self), Validator (cycles) |
| `min_liquidity_buffer_base_minor` | INTEGER | Y | ≥ 0 | USD minor units | | Threshold for `shortfall_flag` | Schema |
| `status` | TEXT | Y | `ACTIVE`, `SUSPENDED`, `CLOSED` | | | `CLOSED` iff `effective_to` is set | Schema |
| `effective_from` | TEXT | Y | Date | | | | Schema |
| `effective_to` | TEXT | N | Date ≥ `effective_from` | | | NULL while not closed | Schema |
| `suspended_from` | TEXT | N | Date > `effective_from` | | | Set iff `status` = `SUSPENDED`; no ledger entry of the entity is dated on or after it | Schema (set iff), Validator (`LIFECYCLE_ACTIVITY_WINDOW`) |

## bank_account  (BankAccount, master)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `bank_account_id` | TEXT | Y | `BANKACC-<entity_id>-nnn` | | PK | Must equal `BANKACC-` + `entity_id` + `-` + `account_seq` zero-padded to 3 digits | Schema |
| `entity_id` | TEXT | Y | FK `legal_entity.entity_id` | | | | Schema |
| `account_seq` | INTEGER | Y | 1–999 | | Unique per entity | Drives the ID suffix | Schema |
| `bank_name` | TEXT | Y | Synthetic bank name | | | | |
| `account_type` | TEXT | Y | `OPERATING`, `COLLECTION`, `DISBURSEMENT`, `PAYROLL`, `RESERVE` | | | | Schema |
| `currency` | TEXT | Y | FK `currency.currency_code` | | | Fixed for the account's life | Schema |
| `opening_balance_minor` | INTEGER | Y | ≥ 0 | account currency minor units | | Balance at the start of `opening_date` | Schema |
| `opening_date` | TEXT | Y | Date ≥ `effective_from` | | | No ledger entry may precede it | Schema (date), Validator (ledger) |
| `status` | TEXT | Y | `ACTIVE`, `DORMANT`, `CLOSED` | | | `CLOSED` iff `effective_to` is set | Schema |
| `effective_from` | TEXT | Y | Date | | | | Schema |
| `effective_to` | TEXT | N | Date ≥ `effective_from` | | | No ledger entry after it | Schema (date), Validator (ledger) |

## currency_rate  (CurrencyRate, master, effective-dated)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `rate_id` | TEXT | Y | `FXR-CCC-yyyymmdd` | | PK | Must equal `FXR-` + `quote_currency` + `-` + `rate_date` without dashes | Schema |
| `rate_date` | TEXT | Y | Date | | With currency pair | One row per non-base currency per calendar day | Schema (unique), Validator (completeness) |
| `quote_currency` | TEXT | Y | FK `currency` | | | ≠ base | Schema |
| `base_currency` | TEXT | Y | FK `currency` | | | Must be the `is_base` currency | Schema (FK), Validator |
| `rate` | REAL | Y | > 0 | base units per 1 quote unit | | Day-over-day move normally < 3%; shock days larger | Schema (>0), Validator |
| `rate_source` | TEXT | Y | `SYNTHETIC_RANDOM_WALK`, `SYNTHETIC_SHOCK` | | | `SYNTHETIC_SHOCK` marks a scheduled shock day | Schema |

## receivable  (Receivable, transaction)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `receivable_id` | TEXT | Y | `RCV-yyyy-nnnnnn` | | PK | Year = `invoice_date` year | Schema |
| `entity_id` | TEXT | Y | FK `legal_entity` | | | Must own `bank_account_id` | Schema (FK), Validator (ownership) |
| `bank_account_id` | TEXT | Y | FK `bank_account` | | | Expected receiving account | Schema |
| `counterparty_name` | TEXT | Y | Synthetic name | | | | |
| `counterparty_risk_tier` | TEXT | Y | `LOW`, `MEDIUM`, `HIGH` | | | Documented driver of delay probability and length | Schema |
| `document_ref` | TEXT | Y | Synthetic invoice number | | Unique per entity | | Schema |
| `currency` | TEXT | Y | FK `currency` | | | Must equal the account's currency | Schema (FK), Validator |
| `amount_minor` | INTEGER | Y | > 0 | currency minor units | | | Schema |
| `invoice_date` | TEXT | Y | Date | | | | Schema |
| `due_date` | TEXT | Y | Date ≥ `invoice_date` | | | | Schema |
| `expected_date` | TEXT | Y | Date ≥ `invoice_date` | | | Scheduled receipt date used by forecasts | Schema |
| `status` | TEXT | Y | `OPEN`, `PARTIALLY_SETTLED`, `OVERDUE`, `SETTLED`, `WRITTEN_OFF`, `CANCELLED` | | | Must equal `to_status` of the latest event | Validator |

Lifecycle (`receivable_status_event`):

| From | Allowed to |
|---|---|
| (none) | `OPEN` |
| `OPEN` | `PARTIALLY_SETTLED`, `OVERDUE`, `SETTLED`, `CANCELLED` |
| `PARTIALLY_SETTLED` | `OVERDUE`, `SETTLED`, `WRITTEN_OFF` |
| `OVERDUE` | `PARTIALLY_SETTLED`, `SETTLED`, `WRITTEN_OFF` |
| `SETTLED`, `WRITTEN_OFF`, `CANCELLED` | terminal |

`OVERDUE` is entered the day after `due_date` if not fully settled. Allowed transitions are checked by the validator, not the schema.

## payable  (Payable, transaction)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `payable_id` | TEXT | Y | `PAY-yyyy-nnnnnn` | | PK | Year = `invoice_date` year | Schema |
| `entity_id` | TEXT | Y | FK `legal_entity` | | | Must own `bank_account_id` | Schema (FK), Validator |
| `bank_account_id` | TEXT | Y | FK `bank_account` | | | Paying account | Schema |
| `payee_name` | TEXT | Y | Synthetic name | | | | |
| `payable_category` | TEXT | Y | `TRADE`, `PAYROLL`, `TAX`, `DEBT_SERVICE`, `CAPEX`, `OTHER` | | | | Schema |
| `is_one_off` | INTEGER | Y | 0, 1 | | | 1 = large non-recurring payment (amount from the one-off size rule) | Schema |
| `document_ref` | TEXT | Y | Synthetic reference | | Unique per entity | | Schema |
| `currency` | TEXT | Y | FK `currency` | | | Must equal the account's currency | Schema (FK), Validator |
| `amount_minor` | INTEGER | Y | > 0 | currency minor units | | | Schema |
| `invoice_date` | TEXT | Y | Date | | | | Schema |
| `due_date` | TEXT | Y | Date ≥ `invoice_date` | | | | Schema |
| `expected_date` | TEXT | Y | Date ≥ `invoice_date` | | | Scheduled payment date | Schema |
| `status` | TEXT | Y | `OPEN`, `PARTIALLY_SETTLED`, `OVERDUE`, `SETTLED`, `CANCELLED` | | | Must equal latest event `to_status` | Validator |

Lifecycle: same as receivable without `WRITTEN_OFF`.

## ledger_entry  (LedgerEntry, transaction, append-only)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `ledger_entry_id` | TEXT | Y | `LED-yyyy-nnnnnnnn` | | PK | Year = `entry_date` year | Schema |
| `bank_account_id` | TEXT | Y | FK `bank_account` | | | `entry_date` within the account's effective window | Schema (FK), Validator |
| `entry_date` | TEXT | Y | Date | | | ≥ account `opening_date` | Schema (format), Validator |
| `direction` | TEXT | Y | `CREDIT`, `DEBIT` | | | Must fit `entry_type` (see below) | Schema |
| `amount_minor` | INTEGER | Y | > 0 | currency minor units | | | Schema |
| `currency` | TEXT | Y | FK `currency` | | | Must equal the account's currency. Stored on the row so a mismatch is observable | Schema (FK), Validator |
| `fx_rate_id` | TEXT | N | FK `currency_rate` | | | NULL only when currency is base. NULL on a non-base row is `FX_RATE_MISSING` | Schema (FK), Validator |
| `base_amount_minor` | INTEGER | N | > 0 | base-currency minor units | | Must equal the conversion formula. NULL only if FX is missing | Schema (>0), Validator |
| `entry_type` | TEXT | Y | `RECEIPT_SETTLEMENT`, `PAYMENT_SETTLEMENT`, `OPERATING_INFLOW`, `OPERATING_OUTFLOW`, `FEE_OR_INTEREST`, `REVERSAL` | | | `RECEIPT_SETTLEMENT`, `OPERATING_INFLOW` = CREDIT. `PAYMENT_SETTLEMENT`, `OPERATING_OUTFLOW` = DEBIT | Schema |
| `external_ref` | TEXT | Y | Synthetic bank reference | | Unique in baseline | Duplicates are only legal under the `DOUBLE_COUNTED_CASH_FLOW` label. Not a UNIQUE constraint so the anomaly can load | Validator |
| `narrative` | TEXT | N | Free text | | | | |
| `reverses_ledger_entry_id` | TEXT | N | FK `ledger_entry` | | | Set iff `entry_type = REVERSAL`; reversed entry must exist, same account and amount, opposite direction | Schema (set-iff), Validator |

## settlement_link  (bridge, Receivable/Payable ↔ LedgerEntry, N:M)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `settlement_link_id` | INTEGER | Y | Sequential | | PK | | Schema |
| `receivable_id` | TEXT | N | FK `receivable` | | With `ledger_entry_id` | Exactly one of `receivable_id` / `payable_id` is set | Schema |
| `payable_id` | TEXT | N | FK `payable` | | With `ledger_entry_id` | | Schema |
| `ledger_entry_id` | TEXT | Y | FK `ledger_entry` | | | Receivable link → `RECEIPT_SETTLEMENT` (CREDIT); payable link → `PAYMENT_SETTLEMENT` (DEBIT) | Schema (FK), Validator |
| `allocated_amount_minor` | INTEGER | Y | > 0 | item currency minor units | | Per ledger entry: sum of allocations ≤ `amount_minor`. Per item: sum of allocations ≤ item `amount_minor` (over-allocation signals double counting) | Schema (>0), Validator |

## receivable_status_event / payable_status_event  (append-only)

Same columns apart from the parent key and the allowed values. Column list below uses the receivable version.

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `event_id` | INTEGER | Y | Sequential | | PK | | Schema |
| `receivable_id` (or `payable_id`) | TEXT | Y | FK parent | | With `event_seq` | | Schema |
| `event_seq` | INTEGER | Y | ≥ 1, gap-free per parent | | With parent | 1 = creation | Schema (≥1), Validator (gap-free) |
| `from_status` | TEXT | N | Status enum | | | NULL iff `event_seq = 1`; must equal previous `to_status`; transition must be allowed | Schema (NULL rule), Validator (chain, transition) |
| `to_status` | TEXT | Y | Status enum | | | ≠ `from_status` | Schema |
| `event_date` | TEXT | Y | Date | | | Non-decreasing per parent | Schema (format), Validator |
| `cause` | TEXT | Y | Receivable: `CREATED`, `PARTIAL_SETTLEMENT`, `FULL_SETTLEMENT`, `DUE_DATE_PASSED`, `WRITE_OFF`, `CANCELLATION`. Payable: same without `WRITE_OFF` | | | Must fit the transition | Schema (enum), Validator |
| `ledger_entry_id` | TEXT | N | FK `ledger_entry` | | | Set for settlement causes; its date = `event_date` | Schema (FK), Validator |
| `actor_role` | TEXT | Y | `SYSTEM`, `CUSTOMER`, `OPERATIONS_ANALYST`, `RISK_COMPLIANCE_REVIEWER`, `SYSTEM_ADMINISTRATOR` | | | `WRITE_OFF` must come from `RISK_COMPLIANCE_REVIEWER` | Schema (enum), Validator |

## liquidity_scenario  (LiquidityScenario, master, versioned)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `scenario_id` | TEXT | Y | `SCN-nnn` | | PK | | Schema |
| `scenario_code` | TEXT | Y | `BASE`, `DELAYED_RECEIPT`, `STRESS` | | With `version_no` | | Schema |
| `version_no` | INTEGER | Y | ≥ 1 | | With `scenario_code` | Changed assumptions → new version, never an edit | Schema |
| `scenario_name` | TEXT | Y | Free text | | | | |
| `assumption_notes` | TEXT | Y | Free text | | | Documents each non-neutral parameter | |
| `receipt_delay_days` | INTEGER | Y | 0–90 | days | | Added to each receivable's expected date | Schema |
| `collection_haircut_bps` | INTEGER | Y | 0–10000 | basis points | | Share of each receivable not collected | Schema |
| `fx_shock_bps` | INTEGER | Y | −5000 to 5000 | basis points | | Applied to every non-base rate (negative = foreign currencies weaken vs USD) | Schema |
| `one_off_multiplier_bps` | INTEGER | Y | 10000–30000 | basis points | | Scales one-off payables. 10000 = ×1.0 | Schema |
| `status` | TEXT | Y | `DRAFT`, `ACTIVE`, `RETIRED` | | | | Schema |
| `effective_from` | TEXT | Y | Date | | | | Schema |
| `effective_to` | TEXT | N | Date ≥ `effective_from` | | | | Schema |

Documented scenario rules, enforced by CHECKs: `BASE` has all four parameters neutral. `DELAYED_RECEIPT` has `receipt_delay_days > 0` and the other three neutral. `STRESS` may combine any of them. The `scenario_code` is the only difference allowed beyond these four numbers.

## forecast_run  (ForecastRun, versioned)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `forecast_run_id` | TEXT | Y | `FCAST-yyyy-mm-dd-nnn` | | PK | Must equal `FCAST-` + `cutoff_date` + `-` + `run_seq` zero-padded to 3 digits | Schema |
| `scenario_id` | TEXT | Y | FK `liquidity_scenario` | | | | Schema |
| `cutoff_date` | TEXT | Y | Date | | With scenario + version | Last date whose data the run may read | Schema |
| `run_seq` | INTEGER | Y | 1–999 | | Via PK | Sequence within the cutoff date | Schema |
| `version_no` | INTEGER | Y | ≥ 1 | | With cutoff + scenario | 1 = first run for that scenario and cutoff | Schema |
| `supersedes_run_id` | TEXT | N | FK `forecast_run` | | | NULL iff `version_no = 1`; same scenario and cutoff as the superseded run | Schema (NULL rule), Validator |
| `horizon_days` | INTEGER | Y | 1–365 | days | | | Schema |
| `horizon_end_date` | TEXT | Y | Date | | | Must equal `cutoff_date + horizon_days` | Schema |
| `base_currency` | TEXT | Y | FK `currency` | | | Must be the `is_base` currency | Schema (FK), Validator |
| `status` | TEXT | Y | `CREATED`, `RUNNING`, `COMPLETED`, `APPROVED`, `REJECTED`, `SUPERSEDED`, `FAILED` | | | Equals latest event `to_status` | Validator |
| `scenario_params_json` | TEXT | Y | JSON object of the four scenario parameters | | | Snapshot at run time; must match the scenario row | Schema (valid JSON), Validator (match) |
| `run_checksum` | TEXT | N | SHA-256 hex | | | Hash of the run's items; same inputs must give the same hash | Validator |

Lifecycle (`forecast_run_status_event`):

| From | Allowed to |
|---|---|
| (none) | `CREATED` |
| `CREATED` | `RUNNING` |
| `RUNNING` | `COMPLETED`, `FAILED` |
| `COMPLETED` | `APPROVED`, `REJECTED`, `SUPERSEDED` |
| `APPROVED` | `SUPERSEDED` |
| `REJECTED` | `SUPERSEDED` |
| `FAILED`, `SUPERSEDED` | terminal |

## forecast_run_status_event  (append-only)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `event_id` | INTEGER | Y | Sequential | | PK | | Schema |
| `forecast_run_id` | TEXT | Y | FK `forecast_run` | | With `event_seq` | | Schema |
| `event_seq` | INTEGER | Y | ≥ 1, gap-free per run | | With run | | Schema, Validator |
| `from_status` | TEXT | N | Run status enum | | | NULL iff `event_seq = 1`; chain and transition checked | Schema, Validator |
| `to_status` | TEXT | Y | Run status enum | | | ≠ `from_status` | Schema |
| `event_date` | TEXT | Y | Date | | | Non-decreasing per run | Schema (format), Validator |
| `cause` | TEXT | Y | `CREATED`, `STARTED`, `FINISHED`, `FAILED`, `REVIEW_APPROVED`, `REVIEW_REJECTED`, `SUPERSEDED` | | | Must fit the transition | Schema (enum), Validator |
| `actor_role` | TEXT | Y | As above | | | `REVIEW_*` must come from `RISK_COMPLIANCE_REVIEWER` | Schema (enum), Validator |

## cash_flow_forecast  (CashFlowForecast, item level, immutable)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `cash_flow_forecast_id` | TEXT | Y | `CF-yyyy-nnnnnn` | | PK | | Schema |
| `forecast_run_id` | TEXT | Y | FK `forecast_run` | | | | Schema |
| `bank_account_id` | TEXT | Y | FK `bank_account` | | | | Schema |
| `source_type` | TEXT | Y | `RECEIVABLE`, `PAYABLE`, `RUN_RATE` | | | Decides which of the two source keys is set | Schema |
| `receivable_id` | TEXT | N | FK `receivable` | | | Set iff `source_type = RECEIVABLE` | Schema |
| `payable_id` | TEXT | N | FK `payable` | | | Set iff `source_type = PAYABLE` | Schema |
| `source_ledger_entry_id` | TEXT | N | FK `ledger_entry` | | | Latest actual the amount depended on (for example a partial settlement). Its `entry_date` must be ≤ the run's `cutoff_date` | Schema (FK), Validator |
| `source_data_date` | TEXT | Y | Date | | | Latest date of any source data used. Must be ≤ `cutoff_date`; later = `FORECAST_USES_ACTUAL_AFTER_CUTOFF` | Schema (format), Validator |
| `direction` | TEXT | Y | `CREDIT`, `DEBIT` | | | `RECEIVABLE` → CREDIT, `PAYABLE` → DEBIT | Schema |
| `currency` | TEXT | Y | FK `currency` | | | Must equal the account's currency | Schema (FK), Validator |
| `expected_date` | TEXT | Y | Date | | | From the source, before scenario adjustment | Schema |
| `forecast_date` | TEXT | Y | Date ≥ `expected_date` | | | Scenario-adjusted. Must be > `cutoff_date` | Schema (≥), Validator (cutoff) |
| `unadjusted_amount_minor` | INTEGER | Y | > 0 | currency minor units | | Outstanding amount before scenario adjustment | Schema |
| `forecast_amount_minor` | INTEGER | Y | ≥ 0 | currency minor units | | After haircut / one-off multiplier | Schema (≥0), Validator (rule) |
| `fx_rate_id` | TEXT | N | FK `currency_rate` | | | NULL only for base currency. Rate date ≤ `cutoff_date` | Schema (FK), Validator |
| `forecast_base_amount_minor` | INTEGER | N | ≥ 0 | base minor units | | Conversion of `forecast_amount_minor`, including FX shock | Schema (≥0), Validator |

Uniqueness of (run, source) is deliberately not a constraint: two items with the same run and source is the `DOUBLE_COUNTED_CASH_FLOW` anomaly and must be loadable.

## daily_cash_position  (DailyCashPosition, derived from ledger)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `bank_account_id` | TEXT | Y | FK `bank_account` | | PK part | | Schema |
| `position_date` | TEXT | Y | Date | | PK part | One row per calendar day from `opening_date`, no gaps | Schema (format), Validator (gaps) |
| `currency` | TEXT | Y | FK `currency` | | | Equals the account's currency | Schema (FK), Validator |
| `opening_minor` | INTEGER | Y | Any integer (overdraft allowed) | currency minor units | | Day 1 = account `opening_balance_minor`; later days = previous `closing_minor` | Validator (`v_daily_position_chain_breaks`) |
| `total_credits_minor` | INTEGER | Y | ≥ 0 | currency minor units | | Sum of that day's CREDIT ledger amounts | Schema (≥0), Validator |
| `total_debits_minor` | INTEGER | Y | ≥ 0 | currency minor units | | Sum of that day's DEBIT ledger amounts | Schema (≥0), Validator |
| `closing_minor` | INTEGER | Y | Any integer | currency minor units | | = `opening + credits − debits`. Mismatch = `OPENING_PLUS_LEDGER_NE_CLOSING` | Validator (`v_daily_position_arithmetic_breaks`, `v_position_vs_ledger_breaks`) |
| `entry_count` | INTEGER | Y | ≥ 0 | count | | Number of ledger rows that day | Schema (≥0), Validator |
| `fx_rate_id` | TEXT | N | FK `currency_rate` | | | Rate for `position_date`; NULL only for base currency | Schema (FK), Validator |
| `closing_base_minor` | INTEGER | N | Any integer | base minor units | | Conversion of `closing_minor`. NULL only when FX is missing | Validator |

## forecast_daily_projection  (derived: projected liquidity per run, entity and day)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `forecast_run_id` | TEXT | Y | FK `forecast_run` | | PK part | | Schema |
| `entity_id` | TEXT | Y | FK `legal_entity` | | PK part | | Schema |
| `projection_date` | TEXT | Y | Date, `cutoff_date < d ≤ horizon_end_date` | | PK part | | Schema (format), Validator (range) |
| `opening_base_minor` | INTEGER | Y | Any integer | base minor units | | First day = entity position at cutoff; later days = previous projected closing | Validator |
| `projected_inflow_base_minor` | INTEGER | Y | ≥ 0 | base minor units | | Sum of that day's CREDIT items for the entity | Schema (≥0), Validator |
| `projected_outflow_base_minor` | INTEGER | Y | ≥ 0 | base minor units | | Sum of that day's DEBIT items | Schema (≥0), Validator |
| `projected_closing_base_minor` | INTEGER | Y | Any integer | base minor units | | = opening + inflow − outflow | Schema |
| `liquidity_buffer_base_minor` | INTEGER | Y | ≥ 0 | base minor units | | Snapshot of the entity's buffer | Schema (≥0) |
| `shortfall_flag` | INTEGER | Y | 0, 1 | | | 1 iff closing < buffer | Schema |

## forecast_vs_actual  (ForecastVsActual, comparison)

| Field | Type | Req | Allowed values / format | Unit | Unique | Validation | Enforced |
|---|---|---|---|---|---|---|---|
| `comparison_id` | TEXT | Y | `VAR-yyyy-nnnnnn` | | PK | | Schema |
| `forecast_run_id` | TEXT | Y | FK `forecast_run` | | | | Schema |
| `cash_flow_forecast_id` | TEXT | N | FK `cash_flow_forecast` | | With `compared_through_date` | NULL = actual with no forecast (unforecast item) | Schema |
| `actual_ledger_entry_id` | TEXT | N | FK `ledger_entry` | | | NULL = forecast not (yet) realised. At least one of the two keys is set | Schema |
| `compared_through_date` | TEXT | Y | Date | | | Must be > run `cutoff_date`. Actuals after this date are not considered | Schema (format), Validator |
| `direction` | TEXT | Y | `CREDIT`, `DEBIT` | | | | Schema |
| `currency` | TEXT | Y | FK `currency` | | | | Schema |
| `forecast_date` | TEXT | N | Date | | | NULL for unforecast actuals | Schema |
| `actual_date` | TEXT | N | Date | | | NULL when not settled | Schema |
| `date_variance_days` | INTEGER | N | Integer | days | | `actual_date − forecast_date`; positive = late | Validator |
| `forecast_amount_minor` | INTEGER | Y | ≥ 0 | currency minor units | | 0 for unforecast | Schema |
| `actual_amount_minor` | INTEGER | Y | ≥ 0 | currency minor units | | 0 when not settled | Schema |
| `forecast_net_base_minor` | INTEGER | Y | Signed | base minor units | | Inflow +, outflow − | Validator |
| `actual_net_base_minor` | INTEGER | Y | Signed | base minor units | | Inflow +, outflow − | Validator |
| `variance_base_minor` | INTEGER | Y | Signed | base minor units | | = actual − forecast. Positive = better cash than forecast | Schema |
| `amount_variance_base_minor` | INTEGER | Y | Signed | base minor units | | Local-amount change valued at the forecast's FX rate | Validator |
| `fx_variance_base_minor` | INTEGER | Y | Signed | base minor units | | Residual: variance − amount variance | Schema (sum identity) |
| `variance_driver` | TEXT | Y | `ON_TARGET`, `TIMING_LATE`, `TIMING_EARLY`, `PARTIAL_OR_SHORT_PAYMENT`, `FX_MOVEMENT`, `UNFORECAST_ONE_OFF`, `UNFORECAST_OTHER`, `NOT_YET_SETTLED`, `SCENARIO_AMOUNT_ADJUSTMENT`, `RUN_RATE_DEVIATION` | | | Derived from source records by a documented priority: unforecast → not settled → short payment (actual below the outstanding amount) → timing (documents only) → amount difference (`SCENARIO_AMOUNT_ADJUSTMENT` for documents whose forecast was haircut or multiplied by the scenario, `RUN_RATE_DEVIATION` for run-rate items) → FX → on target | Schema (enum), Validator |

---

## Relationships and cardinality

| Parent | Child | Cardinality | Optional? | Via |
|---|---|---|---|---|
| currency | legal_entity, bank_account, … | 1:N | No | `*.currency`, `functional_currency` |
| legal_entity | legal_entity (group tree) | 1:N | Parent optional | `parent_entity_id` |
| legal_entity | bank_account | 1:N | An entity may have none | `entity_id` |
| legal_entity | receivable, payable | 1:N | Yes | `entity_id` |
| bank_account | ledger_entry | 1:N | Yes | `bank_account_id` |
| bank_account | daily_cash_position | 1:N | Yes | `bank_account_id` |
| bank_account | receivable, payable | 1:N | Yes | `bank_account_id` |
| currency_rate | ledger_entry, daily_cash_position, cash_flow_forecast | 1:N | Child FK optional (base currency) | `fx_rate_id` |
| receivable ↔ ledger_entry | via `settlement_link` | **N:M** | Ledger entries without a link are operating flows | `receivable_id`, `ledger_entry_id` |
| payable ↔ ledger_entry | via `settlement_link` | **N:M** | As above | `payable_id`, `ledger_entry_id` |
| ledger_entry | ledger_entry (reversal) | 1:0..1 | Yes | `reverses_ledger_entry_id` |
| receivable / payable | status events | 1:N | At least 1 once created | event `*_id` |
| liquidity_scenario | forecast_run | 1:N | A scenario may have no runs | `scenario_id` |
| forecast_run | forecast_run (version chain) | 1:0..1 | Yes | `supersedes_run_id` |
| forecast_run | forecast_run_status_event | 1:N | At least 1 | `forecast_run_id` |
| forecast_run | cash_flow_forecast | 1:N | A failed run may have none | `forecast_run_id` |
| receivable / payable | cash_flow_forecast | 1:N | `RUN_RATE` items have neither | `receivable_id`, `payable_id` |
| forecast_run × legal_entity | forecast_daily_projection | N:M resolved by the composite PK | | `forecast_run_id`, `entity_id` |
| cash_flow_forecast | forecast_vs_actual | 1:N (one per as-of date) | Unforecast actuals have no forecast row | `cash_flow_forecast_id` |
| ledger_entry | forecast_vs_actual | 1:N | Unrealised forecasts have no actual | `actual_ledger_entry_id` |
