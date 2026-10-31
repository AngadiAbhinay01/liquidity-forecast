# Distributions: master data

Every generated value, its distribution, the parameter that controls it (in `src/master/params.py`) and why it is shaped that way. `tests/test_master.py` fails if a parameter in `params.py` is not named here.

**Provenance.** All values are synthetic. No external dataset, market feed or company register was used, so there is nothing to attribute or license. FX levels and company names are invented; the names are built from random syllables and were not checked against real company registers.

**Randomness.** `legal_entity` and `bank_account` draw from the `master` stream, `currency_rate` from the `fx` stream (both derived from the one seed, see `src/rng.py`). Where the data needs guaranteed coverage (every currency used, every lifecycle state present) the choice is a stratified quota rather than an independent coin flip, and the quota is stated below.

## Base currency

**USD** (`Config.base_currency`). Rates are *base units per 1 unit of the quote currency* (USD per EUR). USD is the usual group reporting currency, and storing only the four pairs against USD means cross rates are derived and never stored, so they cannot disagree with each other. Changing the base means changing `Config.currencies` and `INITIAL_RATE`.

## legal_entity

| Field | Distribution | Parameters | Why |
|---|---|---|---|
| `entity_id` | Sequential in generation order | | Brief's ID format |
| `country_code` | Stratified: one country per configured currency first, then categorical by weight (US .22, GB .14, DE .14, FR .06, NL .04, IN .28, JP .12), then the whole list is shuffled | `COUNTRIES` | Guarantees all five currencies appear as a functional currency even in the 6-entity starter; weights give a plausible multinational footprint |
| `functional_currency` | Determined by country | `COUNTRIES` | A country has one currency here (DE, FR, NL → EUR) |
| `entity_type` | 1 `HOLDING`, `max(1, round(0.10·n))` `TREASURY_CENTRE`, rest `OPERATING`; assignment shuffled. n = 1 gives one holding; n = 2 gives holding + operating | `TREASURY_CENTRE_SHARE` | A group needs one root; a treasury centre per ~10 entities mirrors cash-pooling structures |
| `parent_entity_id` | Holding: none. Treasury centre: the holding. Operating: uniform over {holding, treasury centres} | | Tree by construction, so no cycles |
| `legal_name` | Invented: 2–3 random syllables (uniform) + role word (`Group Holdings`, `Treasury Centre`, or one of 10 industry words uniform) + country legal suffix. Re-drawn until unique | `NAME_SYLLABLES`, `NAME_STEM_SYLLABLES`, `INDUSTRY_WORDS`, `LEGAL_SUFFIX` | Readable, unique, not copied from real firms |
| `effective_from` | `date_start − U{365..3650}` days, then raised to the parent's date if earlier | `ENTITY_AGE_DAYS` | Entities pre-date the data window by 1–10 years. The raise means a child never starts before its parent, so several entities share their parent's date |
| `status` | Snapshot as of `date_end`. `SUSPENDED`: `max(1, round(0.08·n))`, `CLOSED`: `max(1, round(0.05·n))`, both only when n ≥ 6, chosen at random from `OPERATING` entities; everything else `ACTIVE` | `SUSPENDED_ENTITY_SHARE`, `CLOSED_ENTITY_SHARE`, `MIN_ENTITIES_FOR_STATUS_QUOTAS` | Guarantees lifecycle coverage. Holding and treasury centres never close, so the group stays connected |
| `effective_to` | Only for `CLOSED`: `date_start + U(0.75, 0.97) · (n_days − 1)` days | `CLOSURE_WINDOW_FRACTION` | Closure in the last quarter leaves most of the year with activity. **Downstream stages must stop an entity's activity at `effective_to`** |
| `min_liquidity_buffer_base_minor` | Lognormal, median USD 250,000, σ = 0.7 (of ln); × 2 for holding and treasury centre; stored in USD minor units | `BUFFER_MEDIAN_USD`, `BUFFER_SIGMA`, `BUFFER_MULTIPLIER` | Positive, right-skewed, so a few entities have much larger buffers. Used by the shortfall flag |

A `SUSPENDED` entity has a `suspended_from` date drawn uniformly from `SUSPENSION_WINDOW_FRACTION` (30%–70%) of the calendar. Its accounts stay `ACTIVE` and keep their balances, but nothing is posted from that date on (the transaction stage ends their posting window the day before). Open receivables and payables of the entity stay open. A closed entity ends at `effective_to` as before.

## bank_account

| Field | Distribution | Parameters | Why |
|---|---|---|---|
| accounts per entity | 1 each, then `n_accounts − n_entities` extra spread by a multinomial with weights holding 1.0, treasury centre 1.5, operating 1.0 | `EXTRA_ACCOUNT_WEIGHT` | Sum is exactly `n_accounts`; mean = 5 at full scale, at least 1 each, treasury centres tend to have more |
| `account_seq` | 1..k per entity | | Drives the ID suffix |
| `account_type` | Account 1 is `OPERATING`. Others: `OPERATING` .30, `COLLECTION` .25, `DISBURSEMENT` .20, `PAYROLL` .15, `RESERVE` .10 | `ACCOUNT_TYPE_WEIGHTS` | Every entity has an operating account; collection/disbursement accounts matter to later receivable and payable stages |
| `currency` | Account 1: entity's functional currency. Others: functional with p = 0.65, else uniform over the other four currencies | `FOREIGN_CURRENCY_SHARE` | Gives FX exposure without making most accounts foreign |
| `bank_name` | Uniform over 12 invented banks | `BANKS` | |
| `opening_date` | 85% (and every account 1) = `date_start`; the rest `date_start + U(0.02, 0.50)·(n_days − 1)` days | `EXISTING_ACCOUNT_SHARE`, `NEW_ACCOUNT_OPEN_FRACTION` | Most accounts exist already; some open in the first half so the ledger has staggered starts |
| `effective_from` | Existing: `max(entity start, date_start − U{30..1500} days)`. New: the opening date | `EXISTING_ACCOUNT_AGE_DAYS` | Never before the entity; satisfies `opening_date ≥ effective_from` |
| `status` / `effective_to` | Accounts of a `CLOSED` entity: `CLOSED` on the entity's `effective_to`. Of the rest (excluding account 1): quota `max(1, round(0.06·n))` `DORMANT` and `max(1, round(0.04·n))` `CLOSED` (closure date as for entities), both only when n_accounts ≥ 15 | `DORMANT_ACCOUNT_SHARE`, `CLOSED_ACCOUNT_SHARE`, `MIN_ACCOUNTS_FOR_STATUS_QUOTAS` | Lifecycle coverage. **Downstream: no ledger entries after `effective_to`; `DORMANT` accounts should carry markedly less activity** |
| `opening_balance_minor` | USD-equivalent ~ lognormal with σ = 0.9, median by type (operating 400k, collection 250k, disbursement 150k, payroll 100k, reserve 800k); × 0.10 for accounts opened after `date_start`; exactly 0 with p = 0.03; converted to the account currency at the opening-date rate and stored in currency minor units | `OPENING_BALANCE_MEDIAN_USD`, `OPENING_BALANCE_SIGMA`, `NEW_ACCOUNT_BALANCE_FACTOR`, `ZERO_BALANCE_SHARE` | Right-skewed amounts; reserves hold the most; a new account starts with initial funding; zero balances are a boundary case |

## currency_rate

One row per non-base currency per calendar day (4 × 365 = 1,460 at full scale). Model, for ln rate `x` on weekday `t`:

`x[t] = x[t−1] + κ·(line[t−1] − x[t−1]) + μ + σ·m[t]·ε[t] + shock[t]`, with `ε ~ N(0,1)`. On Saturday and Sunday the previous rate is carried forward.

| Part | Distribution / value | Parameters | Why |
|---|---|---|---|
| start level (USD per unit) | EUR 1.08, GBP 1.27, INR 0.0120, JPY 0.0067 | `INITIAL_RATE` | Illustrative levels, not market data |
| daily volatility σ | EUR .40%, GBP .45%, INR .20%, JPY .55% | `DAILY_VOL` | Relative ordering resembles major vs managed currencies |
| drift μ | INR −0.01% per weekday, others 0 | `DAILY_DRIFT` | Gentle INR depreciation |
| mean reversion κ | 0.005 per weekday toward the drift line | `MEAN_REVERSION` | Keeps a one-year walk inside a plausible band |
| weekends | No movement | | Markets closed; weekend positions convert at Friday's rate |
| shock dates | `max(1, round(n_days/120))` (3 in a 365-day year) market-wide weekday dates, at least 20 days apart, not within 10 days of either end | `SHOCK_EVERY_DAYS`, `SHOCK_MIN_SPACING_DAYS`, `SHOCK_EDGE_DAYS` | "A few volatility shocks". Same dates for all currencies, like a global risk-off event |
| shock size | `direction · beta · U(2%, 4%)` in ln terms; direction −1 for EUR, GBP, INR, +1 for JPY; beta EUR 1.0, GBP 1.2, INR 0.8, JPY 0.8 | `SHOCK_SIZE`, `SHOCK_DIRECTION`, `SHOCK_BETA` | USD strengthens against most, yen as safe haven. Roughly 5–10σ moves, so unmistakable |
| volatility clustering | Day-after multiplier `m = 1 + 2·exp(−k/3)` for k days after each shock | `POST_SHOCK_VOL_BOOST`, `POST_SHOCK_DECAY_DAYS` | Calm does not return instantly |
| `rate_source` | `SYNTHETIC_SHOCK` on shock dates, otherwise `SYNTHETIC_RANDOM_WALK` | | A forecast variance attributed to FX can point to a rule, not a random flag |
| `rate` | Rounded to 8 decimals | `FX_DECIMALS` | Exact, reproducible text in CSV |

## currency

The five rows come from `Config.currencies` (USD base, EUR, GBP, INR, JPY; JPY has 0 minor-unit digits).

# Distributions: transactions

Parameters live in `src/txn/params.py`; `tests/test_transactions.py` fails if one is not named here. Randomness comes from the `txn` stream. Money is in minor units of the account currency. A USD-equivalent amount is turned into local minor units with that day's FX rate. The smallest document or flow is `MIN_AMOUNT_USD` (USD 50), so a converted base amount is never zero.

**Calendar.** Ledger entries post on business days (Monday–Friday) only; invoices may be dated on any day. A settlement that would fall on a weekend moves to the next Monday. `expected_date` is the next business day on or after `due_date`. Nothing is posted before an account's opening date or after its `effective_to`, and `DORMANT` accounts carry `DORMANT_ACTIVITY_FACTOR` (0.10) of the normal weight in every spread below.

## Ledger composition

| Part | Share of `n_ledger` | Parameter | How the number is reached |
|---|---|---|---|
| Settlements (RECEIPT_SETTLEMENT / PAYMENT_SETTLEMENT) | about `SETTLEMENT_SHARE` = 45% | `SETTLEMENT_SHARE`, `SETTLEMENT_TOLERANCE`, `CALIBRATION_ROUNDS`, `MAX_SETTLEMENT_FRACTION` | The number of trade documents is calibrated: generate, count entries, rescale, repeat up to 4 rounds until within ±10% of target. The generator stops with an error if settlements would exceed 80% of the ledger |
| Receipts vs payments | `RECEIVABLE_SHARE` = 55% of the trade documents are receivables | `RECEIVABLE_SHARE` | Scheduled payables (payroll, debt, tax, one-off) come on top |
| FEE_OR_INTEREST | `FEE_SHARE` = 4%, exactly | `FEE_SHARE`, `FEE_DEBIT_PROB`, `FEE_MEDIAN_USD`, `FEE_SIGMA` | Lognormal amount (median USD 40, σ 0.8); a bank charge (debit) with p = 0.75, else interest income (credit) |
| REVERSAL | `REVERSAL_PAIR_SHARE` = 0.3%, exactly | `REVERSAL_PAIR_SHARE`, `REVERSAL_MAX_GAP_DAYS` | A random regular entry is mirrored: same account and amount, opposite direction, 1–5 days later (next business day), never after the account window, each original reversed at most once |
| OPERATING_INFLOW / OUTFLOW | the remainder, so the ledger has exactly `n_ledger` rows | below | Per-account multinomial on account weight (live days × dormant factor) |

## Receivables

| Field | Distribution | Parameters | Why |
|---|---|---|---|
| account | Weighted by live days over `OPERATING` and `COLLECTION` accounts | | Customers pay into collection or operating accounts |
| counterparty | 12 invented customers per entity, Zipf weights 1/rank, each with a fixed risk tier | `CUSTOMERS_PER_ENTITY`, `RISK_TIER_WEIGHTS` (LOW .50, MEDIUM .35, HIGH .15) | A few big customers and a tail; the tier belongs to the customer, so a late payer stays a late payer |
| `invoice_date` | Uniform from `PRE_WINDOW_DAYS` (45) before the window (only for accounts open at day 0) to one day before the account's last business day | `PRE_WINDOW_DAYS` | January forecasts then have open items. A pre-window invoice is kept only if its first settlement falls inside the account's life |
| payment terms | 15/30/45/60 days with weights .15/.45/.25/.15 | `TERMS_DAYS`, `TERMS_WEIGHTS` | Common net terms; `due_date = invoice + terms` |
| `amount` | Lognormal, median USD 25,000, σ 1.0 | `RECEIVABLE_MEDIAN_USD`, `RECEIVABLE_SIGMA` | Right-skewed invoice sizes |
| outcome | Cancelled with p `CANCEL_PROB` (0.02), 1–10 days after invoice (`CANCEL_WITHIN_DAYS`); otherwise written off with p `WRITE_OFF_PROB` (LOW 0, MEDIUM .01, HIGH .05) at due + `WRITE_OFF_AFTER_DUE_DAYS` (90); otherwise settled | | Lifecycle coverage |
| late payment (**delayed receivables**) | With p `LATE_PROB` (LOW .10, MEDIUM .30, HIGH .60) the settlement is `1 + Geometric` days after `expected_date`, mean `LATE_MEAN_DAYS` (4/9/18), capped at `LATE_MAX_DAYS` (75) | | Risky customers pay late, and later. The actual settlement date is the ledger date; `expected_date` stays as scheduled |
| early payment | Otherwise p `EARLY_PROB` (0.12): 1–`EARLY_MAX_DAYS` (5) days before `expected_date`, not before the day after invoice | | Some customers pay early |
| partial payment | With p `PARTIAL_PROB` (LOW .08, MEDIUM .12, HIGH .20) the amount comes in 2 tranches (3 with p `PARTIAL_THIRD_TRANCHE_PROB` = .30). First tranche is U(`PARTIAL_FIRST_FRACTION`) = (.40,.80) of the amount, the second U(`PARTIAL_SECOND_FRACTION`) = (.30,.70) of what is left (if a third follows), the last the remainder. Gap to the next tranche: `1 + Geometric`, mean `PARTIAL_GAP_MEAN_DAYS` = 12 | | Exercises PARTIALLY_SETTLED. A HIGH-tier partial payer never pays the rest with p `PARTIAL_WRITE_OFF_PROB_HIGH` = .15, and the remainder is written off |

## Payables

| Field | Distribution | Parameters |
|---|---|---|
| trade payables | 12 invented suppliers per entity (Zipf), `OPERATING` / `DISBURSEMENT` accounts, same terms as receivables; amount lognormal median USD 15,000, σ 1.0 | `SUPPLIERS_PER_ENTITY`, `PAYABLE_MEDIAN_USD`, `PAYABLE_SIGMA` |
| payment timing | on time .85, early .10 (1–`PAYABLE_EARLY_MAX_DAYS` = 5 days before), late .05 (geometric, mean `PAYABLE_LATE_MEAN_DAYS` = 7) | `PAYABLE_TIMING_WEIGHTS`, `PAYABLE_EARLY_MAX_DAYS`, `PAYABLE_LATE_MEAN_DAYS` |
| partial / cancelled | 2 tranches with p `PAYABLE_PARTIAL_PROB` = .05; cancelled with p `PAYABLE_CANCEL_PROB` = .02 | |
| payroll | Monthly on `PAYROLL` accounts, paid on the last weekday on or before the `PAYROLL_PAY_DAY` (25th). Account base amount lognormal median USD 60,000, σ 0.4, each month × (1 + N(0, `PAYROLL_MONTH_NOISE` = 2%)) | `PAYROLL_MEDIAN_USD`, `PAYROLL_SIGMA`, `PAYROLL_MONTH_NOISE`, `PAYROLL_PAY_DAY` |
| debt service | `DEBT_SERVICE_ENTITY_SHARE` = 40% of entities; a fixed monthly amount (lognormal median USD 80,000, σ 0.5) on the entity's first operating/disbursement account, due on day `DEBT_SERVICE_DAY` = 5 | `DEBT_SERVICE_ENTITY_SHARE`, `DEBT_SERVICE_MEDIAN_USD`, `DEBT_SERVICE_SIGMA`, `DEBT_SERVICE_DAY` |
| tax | Quarterly, due on day `TAX_DAY` = 15 of the months in `TAX_MONTHS` (Jan, Apr, Jul, Oct); entity amount lognormal median USD 120,000, σ 0.5, ±10% per quarter | `TAX_MEDIAN_USD`, `TAX_SIGMA` |
| **large one-off payments** | `max(ONE_OFF_MIN_COUNT, round(ONE_OFF_PER_ENTITY · n_entities))` = 180 at full scale (3 minimum), on active/suspended entities, `is_one_off = 1`; lognormal median USD 350,000, σ 0.5; terms `ONE_OFF_TERMS_DAYS` = 14; `CAPEX` with p `ONE_OFF_CAPEX_SHARE` = .70, else `OTHER` | `ONE_OFF_PER_ENTITY`, `ONE_OFF_MIN_COUNT`, `ONE_OFF_MEDIAN_USD`, `ONE_OFF_SIGMA`, `ONE_OFF_CAPEX_SHARE`, `ONE_OFF_TERMS_DAYS` |

Scheduled payables are paid on their expected date, in full, and are never batched.

## Batching (one bank entry, several documents)

Single-tranche trade documents of the same account, counterparty and direction whose settlement dates are within `BATCH_WINDOW_DAYS` (4) of the first one are merged with p `BATCH_PROB` (.35) per step, up to `BATCH_MAX_ITEMS` (5) documents. The entry is posted on the latest date, so the earlier documents settle a few days later than planned. This gives real many-to-many `settlement_link` rows. A document is never merged with itself, and partial or scheduled documents are never merged.

## Closure and horizon

Events after the end of the data window are not generated; those documents stay `OPEN` or `OVERDUE`, which is what a forecast needs. On a `CLOSED` account an unsettled receivable that is overdue or partly paid is written off on the closure date; an unsettled, not-yet-due payable with no payments is cancelled; others are left as they are.

## Regular operating flows

| Part | Distribution | Parameters | Why |
|---|---|---|---|
| dates | Uniform over the account's business days, then sorted | | |
| amount | Lognormal, median by account type (OPERATING 9,000 / COLLECTION 12,000 / DISBURSEMENT 8,000 / PAYROLL 4,000 / RESERVE 5,000 USD), σ 0.9 | `REGULAR_MEDIAN_USD`, `REGULAR_SIGMA` | |
| direction (balance feedback) | For each flow the generator looks at the balance so far, `b`, and a target `T = max(opening balance, BALANCE_TARGET_FLOOR_USD (30,000 USD))`. Credit with p = sigmoid((T − b)/(`FEEDBACK_SCALE` · T)) | `BALANCE_TARGET_FLOOR_USD`, `FEEDBACK_SCALE` | Without feedback, balances do random-walk far away from sensible levels. A flow that pushes the balance toward T is multiplied by `1 + |T − b|/T`, at most `REGULAR_MAX_CORRECTION` (4×) |

**This is the only place where a ledger amount depends on a balance.** Every balance that is reported (`daily_cash_position`) is recomputed from the finished ledger, and the reconciliation check recomputes it a second, different way. Settlement amounts do not react to balances, so accounts can still go negative (about 7% of account-days at full scale); that is deliberate, because shortfalls are what the forecast is for.

# Scenarios and forecasts

Parameters live in `src/forecast/params.py`; `tests/test_forecast.py` fails if one is missing here. Nothing in this stage draws a random number except the review outcome of a run (`APPROVED_SHARE`, `forecast` stream). Items, dates and amounts are deterministic functions of the data and the scenario.

## Scenarios (`SCENARIO_DEFINITIONS`, `SCENARIO_NOTES`)

A scenario is exactly four integers. The scenario code, name and id never enter the calculation: the adjustment function receives only the four numbers (a test inspects its signature and source, and a test shows that equal numbers give identical forecasts).

| Scenario | `receipt_delay_days` | `collection_haircut_bps` | `fx_shock_bps` | `one_off_multiplier_bps` | Meaning |
|---|---|---|---|---|---|
| BASE | 0 | 0 | 0 | 10,000 | Receivables on their expected date, in full; payables on their expected date; non-base currencies at the last rate known at the cutoff |
| DELAYED_RECEIPT | 14 | 0 | 0 | 10,000 | Every receivable arrives 14 days later, rolled to the next business day. Nothing else changes |
| STRESS | 21 | 1,000 | −1,000 | 15,000 | Receipts 21 days late and 10% uncollected; every non-base currency worth 10% less in base currency; one-off payments cost 1.5× |

What each parameter touches, and nothing else:

| Parameter | Applies to | Effect |
|---|---|---|
| `receipt_delay_days` | `RECEIVABLE` items only | `forecast_date` = next business day on or after (base date + delay) |
| `collection_haircut_bps` | `RECEIVABLE` items only | amount × (10,000 − haircut) / 10,000, rounded half-even |
| `one_off_multiplier_bps` | payables with `is_one_off = 1` only | amount × multiplier / 10,000, rounded half-even |
| `fx_shock_bps` | every non-base-currency amount, and the opening balance in the projection | rate at the cutoff × (10,000 + shock) / 10,000 |

Run-rate items are not delayed or haircut (they are generic operating flows, not customer promises) but are exposed to the FX shock. Because the item selection cannot depend on the scenario, a delayed receipt can fall after the closure date of its account; the forecast does not model where such money would go.

## Cutoffs and runs

| Item | Rule | Parameter |
|---|---|---|
| cutoff dates | Last business day of each month with a full horizon after it (`horizon_days` = 30 from the config); 11 candidates in 2026 | `CUTOFF_DAY_RULE` |
| runs | Evenly spaced cutoffs: the largest count for which one run per scenario at each, plus one BASE re-run, fits the `n_forecast_items` budget. The rest of the budget is spent at one more cutoff (BASE, DELAYED_RECEIPT, STRESS in turn); the last of those runs is limited to the largest items so the total is exactly `n_forecast_items` (20,000 at full scale) | |
| versions | At the selected cutoff with index `BASE_RERUN_AT_CUTOFF_INDEX` (1) the BASE run is rejected in review and re-run: v1 ends `SUPERSEDED`, v2 (`run_seq` 004, `supersedes_run_id` = v1) is approved. Both versions stay in the tables. Same inputs give the same `run_checksum` | `BASE_RERUN_AT_CUTOFF_INDEX` |
| run history | CREATED → RUNNING → COMPLETED on the cutoff day, then APPROVED the next day with p `APPROVED_SHARE` (0.80), otherwise left COMPLETED. Budget-limited runs stay COMPLETED | `APPROVED_SHARE`, `REVIEW_LAG_DAYS` |

## What a run contains

Everything below uses only records dated on or before the cutoff. A test corrupts every later record and shows the items do not move.

| Item type | Included when | Amount | Date |
|---|---|---|---|
| `RECEIVABLE` / `PAYABLE` | The document exists by the cutoff, is not settled, written off or cancelled according to events up to the cutoff, its account is open at the cutoff, and its expected date (or the day after the cutoff, if already overdue) is inside the horizon. Documents overdue by more than `OVERDUE_MAX_DAYS` (60) are left out. Only documents whose outstanding amount is at least `FORECAST_MATERIALITY_USD` (75,000, at the cutoff rate) are forecast individually | Outstanding amount = document amount − settlements up to the cutoff | `expected_date`, or the first business day after the cutoff if overdue |
| scheduled payables (payroll, debt service, tax, one-off) | Same, but known `SCHEDULE_NOTICE_DAYS` (40) before the payment date, standing in for a commitment calendar. Their documents are dated on the payment day, so without this they would not exist before it | as above | as above |
| `RUN_RATE` | Per live account and direction, from the account's regular operating flows (OPERATING_INFLOW / OUTFLOW) in the last `RUN_RATE_LOOKBACK_DAYS` (90); accounts with fewer than `RUN_RATE_MIN_HISTORY_DAYS` (14) of history are skipped. One item per block of `RUN_RATE_BLOCK_DAYS` (10) days of the horizon | average daily flow × days in the block | the middle of the block, next business day |

Small documents are not forecast, so they appear later as unforecast actuals; that is intended and is what the variance report will measure. `source_ledger_entry_id` is the latest payment (documents) or latest regular flow (run-rate) that the amount depended on; `source_data_date` is the latest source date used. The FX rate of every item is the rate on the cutoff date.

## Projected daily liquidity

`forecast_daily_projection`: for each run, each entity with an open account at the cutoff, each calendar day after the cutoff through the horizon. Opening cash on the first day is the entity's accounts' closing balances at the cutoff, valued at the cutoff rate with the scenario's FX shock; each later day opens at the previous closing. Inflow and outflow are the base-currency amounts of the run's items dated that day (items dated after the horizon are outside the projection). `shortfall_flag` is 1 when the projected closing is below the entity's `min_liquidity_buffer_base_minor`.

# Actual versus forecast

Nothing here is random. Every row, amount and driver is computed from the ledger, the settlement links and the forecast items, so regenerating gives the same table and editing a source record changes the explanation (tests do exactly that).

**Window.** Each run is compared from the day after its cutoff through its `horizon_end_date` (`compared_through_date`). Actuals outside that window are ignored.

**Matching.**

| Forecast item | Actual |
|---|---|
| `RECEIVABLE` / `PAYABLE` | The amounts allocated to that document in `settlement_link` by ledger entries inside the window (a batched entry contributes only its allocation to that document). Actual date and entry = the first such payment; actual amount = their sum |
| `RUN_RATE` | Regular operating flows (OPERATING_INFLOW/OUTFLOW) of the same account and direction inside the item's 10-day block |
| none (no item) | Payments of documents the run did not forecast (one row per document payment), regular flows no run-rate block covers, and all fees, interest and reversals. Each is one row with no `cash_flow_forecast_id` |

Every ledger flow in the window is counted exactly once (a test checks that the rows add up to the ledger's flows).

**Variance** = actual − forecast, in base currency, signed so that a positive number means *more cash than forecast*. Actual base = each payment at the rate of its own day; forecast base = the item's `forecast_base_amount_minor` (cutoff rate with the scenario's shock). `amount_variance` = (actual − forecast amount) in local currency valued at the forecast's rate; `fx_variance` is the remainder, so the two always add up.

**Driver, in priority order** (first rule that fits):

| # | Driver | Rule |
|---|---|---|
| 1 | `UNFORECAST_ONE_OFF` / `UNFORECAST_OTHER` | No item exists. One-off if the payment settles a payable with `is_one_off = 1` |
| 2 | `NOT_YET_SETTLED` | Item exists, no actual in the window |
| 3 | `PARTIAL_OR_SHORT_PAYMENT` | Document items: actual is below the outstanding amount the forecast started from |
| 4 | `TIMING_LATE` / `TIMING_EARLY` | Document items: first payment date after / before `forecast_date` |
| 5 | `SCENARIO_AMOUNT_ADJUSTMENT` / `RUN_RATE_DEVIATION` | Actual amount differs from the forecast amount: for documents because the scenario haircut or multiplied it, for run-rate items because actual flows differ from the trailing average |
| 6 | `FX_MOVEMENT` | Amounts and timing are as forecast but the base value moved |
| 7 | `ON_TARGET` | Nothing differs |

**Metrics** (`derived/forecast_accuracy_*.csv/json`). MAE = mean |variance|. MAPE = mean(|variance| / |actual|) over rows with a non-zero actual. WAPE = Σ|variance| / Σ|actual|; accuracy = 1 − WAPE. Bias = mean variance (and Σvariance / Σ|actual|); negative = the forecast was too optimistic about cash. Each is reported three ways: `item_*` (forecast items that fell due inside the window or were paid in it), `all_flows_*` (also counting unforecast actuals) and `daily_net_*` (per run, entity and day, projection against the ledger, plus the mean absolute error of the projected closing balance).

**Population.** Only runs with status `APPROVED` are scored. A superseded version duplicates its replacement, and unreviewed or budget-limited runs were never accepted as forecasts. All runs are listed in `forecast_accuracy_by_run.csv` with a `scored` flag.

# Anomalies

The issue names and the expected decision for each (`ISSUE_TYPES`, `EXPECTED` in `src/anomalies/params.py`) are listed in the table below. An injector runs after the valid baseline is complete, on its own random stream (`anomaly`). It copies the baseline, changes only the rows it targets, and returns a ground-truth table that is written to `ground_truth/` (never to `input/`). Number of cases of each type: `max(ANOMALIES_PER_TYPE_MIN, round(ANOMALIES_PER_TYPE_FULL × n_ledger / 100,000))`: 10 each (50 in total) at full scale, 3 each at starter scale. Targets of different cases never overlap.

| Type | Table | What changes | Choice of target | Expected decision (reviewer) |
|---|---|---|---|---|
| `OPENING_PLUS_LEDGER_NE_CLOSING` | `daily_cash_position` | `closing_minor` of the *last* position row of an account moves by ±(`CLOSING_ERROR_FRACTION`, 1%–20% of the balance + `CLOSING_ERROR_FLOOR_MINOR`) | Accounts with at least `MIN_POSITION_DAYS` (20) position rows; the last row, so no later row's opening is disturbed and exactly one row breaks | `CORRECT_POSITION_FROM_LEDGER` (risk/compliance) |
| `FX_RATE_MISSING` | `ledger_entry` | `fx_rate_id` and `base_amount_minor` become NULL | Random non-base-currency entry | `REQUEST_FX_RATE_AND_REVALUE` (operations analyst) |
| `DOUBLE_COUNTED_CASH_FLOW` | `cash_flow_forecast` | A new row is appended: same run, source, date and amounts as an existing document item, new ID. The baseline is built `cases` items short of the budget, so baseline + repeats = exactly `n_forecast_items` (the clean baseline has `n_forecast_items` − cases rows; with `--no-anomalies` it has `n_forecast_items`) | Random document-based item | `REMOVE_DUPLICATE_ITEM` (operations analyst) |
| `FORECAST_USES_ACTUAL_AFTER_CUTOFF` | `cash_flow_forecast` | `source_ledger_entry_id` and `source_data_date` point to an actual of the same account dated after the run's cutoff (inside its horizon) | Random item with such an actual, not a duplicate and not a duplicated original | `REJECT_ITEM_AND_ESCALATE` (risk/compliance) |
| `CURRENCY_MIXED_WITHOUT_CONVERSION` | `ledger_entry` | `currency` becomes the base currency while `amount_minor` stays the same number; `fx_rate_id` NULL, `base_amount_minor` = `amount_minor` | Random non-base entry not used by case 2 | `CORRECT_CURRENCY_AND_REVALUE` (risk/compliance) |

Derived reports (`derived/`) are computed from the valid baseline, not from the injected tables. Ground truth columns: `record_id` (for position rows `bank_account_id|position_date`), `table`, `issue_type`, `description`, `expected_decision`, `expected_reviewer_role`, `injected_changes` (before and after of every changed field).


# Exception handling

Detectors (`src/detect/detectors.py`) see only the dataset tables; the ground truth is opened by `src/detect/evaluate.py` alone, after detection. Each detector finds one issue type from the data: position rows whose arithmetic or chain does not reconcile (`reconcile`), non-base rows with no usable FX rate, identical forecast items repeated inside one run, items whose source data, source ledger entry or FX rate is dated after the run's cutoff, and rows whose currency differs from their account's.

| Parameter | Meaning |
|---|---|
| `SEVERITY_ORDER` | LOW < MEDIUM < HIGH < CRITICAL |
| `BASE_SEVERITY` | Level per issue type: cutoff leak CRITICAL (the forecast is invalid); position break, double count and currency mix HIGH; missing FX MEDIUM |
| `SEVERITY_BUMP_USD` | An exception whose base-currency impact is at least this (100,000) goes up one level |
| `SEVERITY_BUMP_CAP` | The size bump stops at HIGH: only a cutoff leak is CRITICAL |
| `RECOMMENDED_DECISION` | The playbook decision per issue type; same wording as the injector's expected decisions, but kept separately so the detector side never imports the injector |
| `RISK_COMPLIANCE_TYPES` | Types routed to risk/compliance (position break, currency mix). CRITICAL items also go there. Everything else goes to the operations analyst |
| `DISMISS_DECISION` | `DISMISS_AS_FALSE_POSITIVE`, which only risk/compliance may record |

Queue rows are `exception_id` (`EXC-000001`, in sorted order of issue type, table and record), type, record, severity, impact, evidence, recommended decision, assigned role, status (`OPEN`, `ESCALATED`, `DECIDED`) and the reviewer decision fields. The decision log has one row per event (`DETECTED`, `ROUTED`, `ESCALATED`, `DECISION_RECORDED`); it is append-only in Python (no edit path) and in SQLite (`sql/exceptions.sql`, triggers). The shipped queue has no decisions: reviewing is a human step. `python -m src.detect.evaluate` reports precision, recall and F1 per issue type, micro and macro averages, routing accuracy and decision accuracy.
