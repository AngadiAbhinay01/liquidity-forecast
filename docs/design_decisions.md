# Design decisions

Short rationale for each non-obvious choice, with the trade-off. Items marked **Assumption** are my reading of an ambiguous brief and should be confirmed.

## Structure

1. **Integer minor units for money.** Reconciliation then compares exact integers, with no float drift. Cost: every consumer must apply `minor_unit_exponent` (JPY = 0). FX rates are the only REAL column; stored base amounts are materialised at generation, so reconciliation never depends on re-multiplying floats.
2. **Amounts positive, `direction` carries the sign.** Sign errors become a visible, checkable column instead of a negative number hiding in a sum. Cost: sums need a `CASE`.
3. **Receivable and Payable are separate tables.** Their lifecycles differ (`WRITTEN_OFF` exists only for receivables) and a forecast item points at exactly one. A single "item" table with a type column would need more conditional CHECKs.
4. **One bridge table, `settlement_link`, with two nullable parent keys** and an exactly-one CHECK. Two bridges would duplicate the same four columns. The ledger side stays a single FK.
5. **Ledger entries are immutable.** Corrections are `REVERSAL` rows. Triggers abort UPDATE and DELETE.
6. **Status history lives in event tables; `status` on the parent is a cached current state.** The validator checks `status` equals the latest event's `to_status`. Events carry `actor_role`, which ties the four actors in the brief (customer, operations analyst, risk/compliance reviewer, system administrator) to recorded decisions.
7. **A forecast run is one scenario at one cutoff.** The three scenarios at the same cutoff are three runs, so "scenarios differ only by documented assumptions" is testable by comparing runs with equal cutoff. A re-run is a new row with `version_no + 1` and `supersedes_run_id`; nothing is overwritten. `scenario_params_json` snapshots the assumptions in force.
8. **Scenario parameters are four integer columns, not free text.** `BASE` and `DELAYED_RECEIPT` have CHECKs that pin the other parameters to neutral, so a scenario cannot silently differ by an undocumented lever.
9. **Forecast items store both the unadjusted and the adjusted figures** (`expected_date`/`forecast_date`, `unadjusted_amount_minor`/`forecast_amount_minor`). The effect of a scenario on any single item can be read off the row.
10. **Variance is decomposed.** `forecast_vs_actual` splits base-currency variance into an amount effect and an FX effect, with FX as the residual so the split always sums. A single `variance_driver` is chosen by a fixed priority rule, so it is derived rather than assigned.
11. **`daily_cash_position` stores `closing_minor` rather than computing it.** A generated column would make a reconciliation break impossible to represent. The arithmetic and chain checks are views.
12. **Consolidation is a view** (`v_daily_entity_position`), not a table. It adds no state that could drift from the account rows. `accounts_missing_fx` stops a total with a missing rate from looking complete.

## Constraints versus validators

13. **Schema enforces structure; validators enforce business rules.** CHECKs cover formats, enums, signs, row-level date order and ID-to-column agreement (for example the `BANKACC-` ID must equal entity + sequence, and the `FCAST-` ID must equal cutoff date + sequence). Reconciliation, currency match, forecast cutoff and duplicates are views and Python validators. The reason: the brief requires five labelled anomalies, and each would be rejected at insert if it were a constraint. The cost is that a hand-written bad row loads silently, so the validators matter and `tests/test_schema.py` pins which rules are which.
14. **Anomalies are injected in memory before load.** The append-only triggers would block post-load UPDATEs, and that is intended.
15. **Ground truth is outside the database.** It is a separate file so no input table can carry a label column.

## Assumptions to confirm

16. **Assumption: `BANKACC-<entity-id>-001` is `BANKACC-ENT-000001-001`.** I took `<entity-id>` as the whole entity ID. If the intended form is `BANKACC-000001-001`, change the one CHECK in `bank_account` and the ID helper.
17. **Assumption: the year in `LED-`, `CF-`, `RCV-`, `PAY-` IDs is the year of the record's own date,** with the counter restarting each year. The brief shows only 2026 examples. If the intended counter is global, change the helper; no table structure changes.
18. **Assumption: USD is the base currency; five currencies are USD, EUR, GBP, INR, JPY.** Only the `currency` seed rows and config change if you pick others.
19. **Assumption: receivable, payable and ledger currency equals the account currency.** This keeps settlement exact and makes "currency mixed without conversion" a clean, detectable anomaly. Cost: no cross-currency settlement is modelled.
20. **Assumption: one-off payables use a flag (`is_one_off`)** rather than a separate table. "Large" is a generation rule, not a column.

## Known simplifications

21. **No event tables for entity, account or scenario state.** Those use effective dates (`effective_from`/`effective_to`) and a status. In the synthetic data they change at most once (closure), so evented history would add tables without adding signal. The brief's "retain event history for state changes" is met for the entities where state actually changes during the data window: receivables, payables and forecast runs.
22. **Exception queue and decision log are not here yet.** They arrive with the detection step (Prompt 9) and will be separate tables or files, not part of the input data.
23. **Mermaid diagram was written to the syntax but not rendered in this session.** Paste it into any Mermaid viewer to confirm before committing.

## Transactions (Prompt 5)

24. **The plan is drawn first, the records are derived from it.** Each receivable/payable gets a plan (settlement tranches, optional write-off/cancel). Ledger rows, `settlement_link`, the status history and the stored `status` all come from that one plan, so they cannot disagree. Cost: `status` is never "drawn", so it cannot be set independently to test a mismatch; the anomaly stage will inject that on purpose.
25. **Only one place lets a ledger amount depend on a balance.** Regular operating flows steer the balance toward a target (`txn/ledger.py`, `FEEDBACK_SCALE`). Without it, 100k flows random-walk to absurd balances. Every reported balance is still recomputed from the finished ledger, and the reconciliation check recomputes it again a different way. Settlements do not react to balances, so about 7% of account-days are negative; that is intended.
26. **Batching is limited to single-tranche trade documents.** Partial and scheduled documents are never merged, so tranche order inside a document cannot be reversed by a batch. The entry is posted on the latest date, which delays the earlier documents by up to 4 days; the plan's tranche day is overwritten to match.
27. **A second partial tranche writes no status event.** `from_status <> to_status` is a table CHECK, and `PARTIALLY_SETTLED -> PARTIALLY_SETTLED` is not a change. The payment is still in `settlement_link` and the ledger; only the history row is absent.
28. **Closure rules.** Nothing is posted after `effective_to`. On a closed account an overdue or part-paid receivable is written off on the closure date and an unpaid, not-yet-due payable with no payments is cancelled; others stay as they are rather than invent a transition the lifecycle does not allow.
29. **Base-currency account rows carry no `fx_rate_id`** (rate is 1, `base_amount_minor = amount_minor`). Non-base rows always carry both. The "FX rate missing" anomaly (Prompt 8) can therefore only be injected where a rate is expected.
30. **Ledger numbering happens after sorting.** IDs are issued per year in (date, account, creation order), so `LED-2026-00000001` is the earliest posting, and `external_ref` (`BNK` + creation order) is unique in the baseline. The duplicate-count anomaly will reuse an `external_ref`.
31. **Known gap in the tests.** A mutation that posts a batch on its earliest instead of latest date survives: the effect is invisible in the tables (there is no stored "planned" date for a batched document). It is a modelling choice, not an invariant.

## Scenarios and forecasts (Prompt 6)

32. **Scenario-independent selection.** Which documents become items, in what order, and with which source keys is decided before any scenario is applied; the scenario function only changes four things (dates of receivables, amounts of receivables and one-offs, base values under an FX shock). Cost: a delayed receipt can land after its account closed.
33. **The exact item budget is spent by truncation, not by changing data.** The number of items per run is whatever the data gives, so the last run is limited to its largest items to reach exactly `n_forecast_items`. Cost: one run covers fewer exposures than its siblings; it stays a subset of the full ranking and ends `COMPLETED`, never approved.
34. **Materiality threshold and run-rate.** Only open documents of at least USD 75,000 are forecast one by one; regular operating flows are forecast as run-rate blocks from the trailing 90 days. This keeps the item count inside the 20,000 budget over several cutoffs and gives the later variance report realistic unforecast items. It means about 70% of the items are run-rate.
35. **Scheduled commitments are visible 40 days ahead.** Payroll, debt service, tax and one-off payments are stored as documents dated on the payment day; a commitment calendar stands in for knowledge that would exist earlier. Without it a forecast could not see next month's payroll without breaking the cutoff rule.
36. **Re-run (v2) is a review outcome, not new data.** The BASE run at one cutoff is rejected and re-run with the same inputs; v2 therefore has the same checksum as v1. It shows version chaining and non-overwriting storage; it is not a different forecast.
37. **FX in a forecast is the rate at the cutoff.** No future rate is predicted; actual rate movement after the cutoff is what the FX part of the variance report will isolate. The FX shock also moves the opening balance, otherwise a stress would revalue flows but not the cash they add to.

## Actual versus forecast (Prompt 7)

38. **Two extra driver values.** The schema now allows `SCENARIO_AMOUNT_ADJUSTMENT` and `RUN_RATE_DEVIATION`. Without them, a stress forecast that deliberately haircuts a receivable would be labelled a "short payment" when the customer paid in full, and every run-rate item would be labelled something it is not. Cost: the enum is larger than the brief's examples.
39. **"Short payment" is judged against the outstanding amount, not the scenario-adjusted forecast.** That makes the driver independent of the scenario: the same customer behaviour gets the same label in BASE and STRESS.
40. **Run-rate items are compared with block totals.** A 10-day run-rate item cannot be matched to individual entries, so its actual is the sum of regular flows in the block. Timing is therefore not a driver for run-rate items.
41. **Forecast accuracy is modest on purpose-built data, and that is reported, not tuned away.** Item-level accuracy is roughly 0.6; daily net accuracy is negative because daily cash is noisy and most actual flows (small documents, fees, regular flows) are not forecast individually. I did not adjust the generator to make the forecast look better.
42. **Only APPROVED runs are scored,** so the superseded v1 and the budget-limited run do not distort the scenario metrics. This also drops the roughly 20% of runs left unreviewed by chance.

## Anomalies (Prompt 8)

43. **`generate_all` stays the clean baseline; `generate_dataset` adds the anomalies.** Every earlier test and report keeps working on valid data. The injected tables are what the CLI publishes as `input/`; `--no-anomalies` publishes the baseline.
44. **Case 1 targets the last position row of an account.** Changing a mid-series closing would also break the next row's opening (a chain break), so one injection would show up as two issues. The last row breaks exactly one thing.
45. **Case 3 adds rows.** The only way to count something twice without deleting or editing an unrelated record is to append the repeat, so the baseline is generated `cases` items short of the budget (19,990 at full scale) and the published input has exactly 20,000 forecast items after the repeats (revised from the first version, which published 20,010). With `--no-anomalies` the clean baseline has the full 20,000. `forecast_daily_projection` and `forecast_vs_actual` are *not* updated for the repeat: they are derived tables, and their disagreement with the items is one more signal a detector could use.
46. **Case 5 re-labels the currency as USD and keeps the number.** The schema has no constraint tying a ledger entry's currency to its account, on purpose (a CHECK would make this impossible to load). The row then looks like a perfectly normal USD entry except that its account is not USD.
47. **Reports are computed from the baseline.** The accuracy and consolidation reports describe the valid data; computing them from injected tables would mix real forecast error with injected errors.
48. **Ground truth has two extra columns** beyond the five requested: `expected_reviewer_role` (used by the exception workflow) and `injected_changes` (before and after of every changed field, so a test can prove nothing else moved).

## Exception handling (Prompt 9)

49. **Detectors work from rules a reviewer could state, not from knowledge of the injector.** Each looks for a violated invariant (arithmetic, FX presence, repeated key, date after cutoff, currency vs account). The tests include anomalies made by hand and near misses, so a perfect score is not just the injector and detector agreeing with themselves.
50. **A repeat is flagged, the first occurrence is not.** Among identical items the lowest id is treated as the original. If a duplicate happened to sort first, the "wrong" row would be flagged; for a reviewer the pair is the same problem, but the evaluation matches on record id, so this is a stated limit.
51. **Severity is a base level per type plus a size bump, capped at HIGH.** Only a cutoff leak is CRITICAL because it invalidates a forecast rather than misstating one number.
52. **Routing is a rule, not a lookup of the answer.** Risk/compliance gets CRITICAL items and the two types that touch reported balances or currency; the rest goes to the operations analyst. It agrees with the ground truth's expected role, and the evaluation reports that as an accuracy rather than assuming it.
53. **Authority is enforced in code and the log is append-only in code and SQL.** The analyst can decide only items assigned to that role and can escalate; only risk/compliance can dismiss. One decision per exception. The shipped queue is left undecided; I did not invent reviewer decisions.
54. **The evaluation matches on (issue type, table, record id).** Detecting the right issue on the wrong record counts as one false positive and one miss.

## Validation (Prompt 10)

55. **Schema rules are read from the schema file, not copied.** Keys, foreign keys, NOT NULL and enums are taken from `sql/schema.sql` and applied to the DataFrames, so the validator and the database cannot drift. The regex that extracts enums is covered by tests (including a `NOT IN` that must not be mistaken for an enum).
56. **Business rules are written twice, in different languages.** The `v_*` SQL views and the pandas checks state the same invariants separately. A reviewer who trusts neither can compare them.
57. **Lifecycle tables are duplicated on purpose,** with a test that they equal the generator's. If the validator imported the generator's table, a wrong transition would validate itself.
58. **Checks never crash on bad data.** A duplicated key used to raise inside unrelated lookups; lookups now keep the first row. A check that still raises is reported as a failure.
59. **"Caught only where labelled" is measured, not assumed.** `labels.py` lists flagged records that are not in the ground truth; the test requires that list to be empty, and also requires every labelled case to trip its designated check.
60. **Exit code 1 on the shipped dataset is intended.** It contains labelled anomalies. The clean variant exits 0.

## Exports and reproducibility (Prompt 11)

61. **The generator was not reproducible across processes, and the new check found it.** Customer and payee names were built in a Python `set`, then given random sort keys in set-iteration order. Set order of strings depends on the per-process hash seed, so two runs with the same seed produced different receivables, ledger entries and everything downstream. In-process tests could not see it. Fix: sort the set before drawing keys. A regression test now runs separate processes with different `PYTHONHASHSEED` values and requires identical file hashes. Prompts 1-10 as first delivered contain the bug.
62. **The comparison runs the generator in separate processes** (`subprocess`) in fresh directories, not twice in one interpreter, for the reason above.
63. **A sensitivity control is part of the check.** The script also generates with seed+1 and requires the data files to differ. A comparison that cannot fail proves nothing; the test suite also confirms the script fails on a changed file, a missing file and a row-count change.
64. **Four formats, one source.** CSV, JSON, SQL seed and the SQLite file are written from the same DataFrames; the check requires the same row count for every table in all four, and tests load the seed and the database and compare every row.
65. **The manifest lists only generator-owned files.** Output of later steps (`exceptions/`, `validation/`, `evaluation/`) can sit in the same directory without changing the manifest on a re-run. The manifest has no timestamps or paths, and includes the library versions because byte-identical output is only promised for the pinned environment.
66. **SQLite files are byte-reproducible here** (same SQLite build, `VACUUM` after load). Across different SQLite versions the file bytes may differ even when the content is identical; the row-level comparison in the tests is the portable guarantee, the checksum is the same-environment guarantee.

## Fixes after the audit (round 1)

67. **Published forecast table has exactly `n_forecast_items` rows (audit H2).** The baseline reserves room for the repeats case 3 appends.
68. **Suspension is now a dated lifecycle state (audit H3).** `legal_entity.suspended_from` is set exactly when status is `SUSPENDED`; the transaction stage ends the posting window of the entity's accounts the day before, and a validator (`LIFECYCLE_ACTIVITY_WINDOW`) fails any ledger entry on or after that date. Balances and open documents of the suspended entity stay on the books.
69. **Large one-off payments are four times as many (audit H6):** 6 per entity (180 at full scale) instead of 1.5. The one-off lever of the STRESS scenario and the `UNFORECAST_ONE_OFF` driver now have something to act on.
70. **Account ID format confirmed against the problem statement:** `BANKACC-<entity-id>-001` with `<entity-id>` = `ENT-000001` gives `BANKACC-ENT-000001-001`; ledger and forecast IDs carry the year as in the examples (audit M4).
