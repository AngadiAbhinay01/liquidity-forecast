# Audit, round 1 (nothing fixed yet)

Method: I regenerated the full dataset (`--scale full`, seed 20261022, after the Prompt 11 fix) and queried it, ran the suite (591 tests passing) and read the code and docs against the problem statement and the evaluation focus (realistic distributions, consistent IDs and joins, lifecycle consistency, correct derived values, edge-case coverage, separate ground truth, reproducibility, documentation). Every finding below has a number or a command behind it. Limits: I worked from my summary of the problem statement, not the PDF, so please check section 1 against the original. I wrote most of this code, so I am likely to under-see its blind spots; a second reader would find more.

## Ranked findings

### HIGH

**H1. Submission deliverables are not in the repo yet.** There is no README, no `prompts/` folder with the prompt history, no AI-use disclosure, no `.gitignore`/LICENSE, no git history, and no committed starter data. These are explicit requirements, and a reviewer opening the repo today sees none of them. (Prompt 13 covers README and `ai_usage.md`; the prompt history, git history and starter data commit are yours to do.)

**H2. Shipped input has 20,010 forecast items, not 20,000.** The brief asks for 20,000 items. The baseline has exactly 20,000, but anomaly case 3 appends repeats, so `input/cash_flow_forecast.csv` has 20,010 rows (3 extra at starter scale). Check: `wc -l data/full/input/cash_flow_forecast.csv` gives 20,011 lines. A reviewer counting rows will see a mismatch. Design decision 45 documents it, but the number is still wrong for a strict check.

**H3. A SUSPENDED entity trades all year.** `ENT-000016` and `ENT-000023` are SUSPENDED with no end date and post 2,228 and 3,921 ledger entries from 2026-01-01 to 2026-12-31 (6,149 rows, about 6% of the ledger). Nothing in the lifecycle restricts activity by status, and no validator checks it. This is a lifecycle-consistency gap in exactly the area the evaluation names.

**H4. Cash-flow patterns are not realistic where a treasury reviewer looks first.**
* Payroll is 1.1% of payables and 2.2% of payable forecast items (38 of 1,752); trade is 74%, tax 12.5%, debt service 7.5%. There are no monthly payroll, tax or loan-service cycles.
* Ledger volume per month is nearly flat (7,442 to 8,881), with no month-end or quarter-end pattern.
* Zero weekend ledger entries (0.0%) and every business day has entries (261 of 261). Receivable due dates fall on weekends 28.7% of the time, and FX rates exist for weekends (28.5% of rate rows), while no cash ever moves on those days.
* One global business-day calendar for all countries (India, Japan and the UK have different holidays).

**H5. Overdrafts are an artefact, not a modelled feature.** 71 of 150 accounts go negative at some point; 6.0% of account-days are negative; the worst balance is -5.17 billion minor units (account `BANKACC-ENT-000022-007` is negative for 215 days). There is no overdraft facility, so the "consolidated liquidity" reports are partly driven by random operating flows overdrawing accounts. Prompt 5's balance feedback reduced this but did not remove it.

**H6. The "one-off payment multiplier" scenario lever is almost inert.** Only 0.2% of payables are one-off (60 of 1,752 payable forecast items); the variance driver `UNFORECAST_ONE_OFF` occurs once in 87,939 comparison rows. STRESS therefore differs from BASE through the delay, haircut and FX levers, and the brief's own example driver (a one-off payment) is nearly absent from the explanations. A test proves scenarios differ only through parameters; it does not prove each parameter does something visible.

### MEDIUM

**M1. Forecast quality is poor and mostly unexplained.** Item-level accuracy is 0.57 to 0.61. Daily-net accuracy is negative for all scenarios (-0.24, -0.37, -0.30), meaning the forecast is worse than forecasting zero. 77% of comparison rows are `UNFORECAST_OTHER`, and 69% of forecast items are `RUN_RATE` blocks rather than receivable or payable documents (documents are only 31%). Reported honestly (decision 41), but a reviewer may read the explained-variance goal as under-delivered.

**M2. The run calendar is irregular and scenario metrics are not like-for-like.** Cutoffs exist only on 6 dates (Jan 30, Feb 27, Mar 31, Jun 30, Sep 30, Nov 30); April, May, July, August, October and December have none. The Feb run is budget-truncated to 219 items and left COMPLETED (not scored). STRESS is scored on 4 runs, BASE and DELAYED_RECEIPT on 5, so the "by scenario" comparison uses different run sets.

**M3. Evaluation scores are partly circular.** Detection F1, routing accuracy and decision accuracy are all 1.000 at both scales. The anomalies are single-field edits on easy targets (last position row, a null FX column, an appended exact copy), the labelled "expected reviewer role" and the router's `RISK_COMPLIANCE_TYPES` encode the same table, and detectors and validators were written by the same author who wrote the injector. The tests include hand-made anomalies and near-misses, but the headline 1.0 should not be presented as evidence of robustness. There is no noisy or ambiguous case (partial duplicates, a near-duplicate with a different amount, an FX rate that exists but is stale).

**M4. Unconfirmed identifier assumptions.** `BANKACC-ENT-000001-001` (entity id embedded in the account id) and per-year counters (`LED-2026-…`, `CF-2026-…`) are interpretations of the brief that nobody has confirmed. The ledger covers only 2026, so the per-year counter is never exercised across a year boundary. If the interpretation is wrong, ID regexes, schema CHECKs, validators, docs and tests all change.

**M5. A known test gap remains.** The mutation "batched documents use the minimum date instead of the batch date" survives the suite (design decision 31). Documented, not closed.

**M6. The test suite is slow and repetitive.** 591 tests take about 4 minutes. Several modules each build full-scale fixtures; there is no shared session fixture and no CI configuration, so nobody but you will run it.

**M7. Delayed receipts can be forecast beyond the horizon or after account closure.** In the starter baseline 15 forecast items have `forecast_date` later than the run's horizon end; the schema allows it and the validator does not flag it. Documented as a limit in Prompt 6, but it is still a forecast item pointing at a date outside its own run.

### LOW

* **L1. Stale text.** `src/export.py` opens with "CSV now; JSON, SQL seed and SQLite are added with the export step". `src/generate.py`'s docstring says "tables built so far". The ER diagram is Mermaid source that was never rendered, so its syntax is unchecked.
* **L2. Environment is under-specified.** `requirements.txt` pins numpy, pandas and pytest but not Python (3.13 was used), and there is no `pyproject.toml`; the pinned pandas may not install on other Python versions.
* **L3. SQLite file bytes are only reproducible with the same SQLite build.** Content is identical across builds (the tests compare rows); the checksum is not.
* **L4. Amount scale.** Amounts are integer minor units across currencies, so `SUM(amount_minor)` over mixed currencies is meaningless (JPY has exponent 0). The data dictionary says so, but a naive user will make the mistake. The tail is heavy: the largest single ledger entry is 10.1 billion minor units (about 101 million in a 2-decimal currency).
* **L5. Detectors and validators implement the same five rules twice** (plus SQL views). This is deliberate for independence, but it means any rule change touches three places. `src/validate/__main__.py` imports `INPUT_TABLES` from `src/detect/__main__.py`, an odd dependency direction.
* **L6. Housekeeping.** `__pycache__` directories sit in the tree and there is no `.gitignore`.

## Things I checked that held up

* Baseline passes all 30 validation checks at starter and full scale; injected data flags exactly the labelled records (3 and 10 per type) and nothing else.
* Ground-truth columns appear in no input table, JSON, SQL seed or the SQLite file (tested), and detection and validation output is unchanged when the ground truth is corrupted or deleted.
* Same-seed output is byte-identical across separate processes and different `PYTHONHASHSEED` values (after the Prompt 11 fix), at starter and full scale; CSV, JSON, SQL seed and SQLite row counts agree for all 17 tables.
* Every FK resolves, every ID matches its documented format, position arithmetic and ledger reconciliation hold on every account-day.

## Suggested order when fixes begin

1. H2 and M4 first: they decide the numbers and IDs that everything else repeats, so they are cheapest to change now.
2. H3, H5, H6, H4 next: realism and lifecycle changes alter the generated data, so fix them together and regenerate once.
3. M1 to M3, M7 after the data stops moving.
4. H1 and the Low items last (Prompt 13 covers part of H1).

## Status after the first round of fixes

| Finding | Status |
|---|---|
| H1 submission deliverables | README, `ai_usage.md` and a local git history now exist; pushing to GitHub, the `prompts/` folder and your own notes are still yours to do |
| H2 20,010 forecast items | Fixed: the published table has exactly 20,000 rows (baseline 19,990 + 10 repeats) |
| H3 suspended entity trades all year | Fixed: `suspended_from` column, no posting from that date, validator `LIFECYCLE_ACTIVITY_WINDOW` |
| H4 flat, weekday-only cash flows | Open |
| H5 overdrafts | Open |
| H6 one-off lever inert | Improved: 180 one-off payables (was 45), 252 of 1,940 payable forecast items are one-offs (was 60 of 1,752). The `UNFORECAST_ONE_OFF` driver is still rare because one-offs are mostly forecast in advance |
| M1 to M3, M5 to M7 | Open |
| M4 ID assumptions | Confirmed against the problem statement |
| L1 stale docstrings | Fixed in `export.py` and `generate.py` |
