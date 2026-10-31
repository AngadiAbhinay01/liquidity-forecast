# Prompt history

All prompts I sent to the AI tool (Claude, in Claude Code) while building this project. Responses are not included (optional per the FAQ).

## Prompts 1

I'm doing a 72-hour code challenge: a synthetic data generator plus liquidity forecasting
workflow. Problem: consolidate account balances and expected cash flows to forecast
short-term liquidity under scenarios.

Entities: LegalEntity, BankAccount, LedgerEntry, CashFlowForecast, Receivable, Payable,
CurrencyRate, LiquidityScenario, ForecastRun.
IDs: ENT-000001; BANKACC-<entity-id>-001; LED-2026-00000001; CF-2026-000001;
FCAST-2026-01-01-001.
Volume: 30 entities, 150 bank accounts, 100,000 ledger entries, 20,000 forecast items,
5 currencies. Fixed seed 20261022, scale parameterized.
Edge cases to inject with labels: opening + ledger != closing; missing FX rate; cash flow
counted twice; forecast uses actuals after cutoff; mixed currency without conversion.
Acceptance: daily balances reconcile, scenarios differ only by documented assumptions,
forecasts reproducible, actual-vs-forecast variance explainable.

Do NOT write code yet. Propose: (1) repo structure, (2) tech stack (Python, pandas, SQLite,
pytest, no heavy deps), (3) generation order, (4) key assumptions and trade-offs,
(5) risks. Ask me any clarifying questions.

## Prompts 2

Based on the plan, write docs/data_dictionary.md and a SQL schema (schema.sql) for all 9
entities. For every field give: type, required/optional, allowed values, units, uniqueness,
validation rule. Define PKs and FKs, cardinality (1:1, 1:N, N:M), optional relationships,
effective dates, lifecycle states.
Also add: (a) a bridge table for the N:M relationship (e.g. settlement links between
Receivable/Payable and LedgerEntry), (b) append-only event/history tables for state
changes (receivable status, forecast run status) so history is never overwritten,
(c) a daily_cash_position table, (d) a forecast-vs-actual comparison table.
Add an ER diagram in Mermaid. Explain each design decision briefly.

## Prompts 3

Create the project skeleton: src/ package, config.py (seed=20261022, scale parameters:
n_entities, n_accounts, n_ledger, n_forecast_items, currencies, date range), a CLI
(python -m src.generate --scale full|starter --seed N --out data/), requirements.txt, and
a deterministic ID helper module implementing the 5 ID formats with zero-padded counters.
Use a single numpy Generator seeded from config; no global random state or wall-clock
time anywhere. Add unit tests for the ID helpers.

## Prompts 4

Implement generation of master tables in dependency order: LegalEntity (30, with
functional currency, country, lifecycle state), BankAccount (150, ~5 per entity, valid
entity FKs, account currency from the 5 currencies, opening balance and opening date),
and CurrencyRate (daily FX rates for the full date range with realistic random-walk
movement and a few volatility shocks, base currency chosen and documented). Every
generated value must have a documented distribution. Add tests for key uniqueness and
FK validity.

## Prompts 5

Implement Receivable and Payable (scheduled amounts, expected dates, due dates,
counterparties, status lifecycle, some delayed receivables with actual settlement date
later than expected, some large one-off payments). Then LedgerEntry (100,000 rows):
settlements of receivables/payables plus other operating flows, each with valid parent
IDs. Then compute from source records, not independently generated: running balances,
closing balances, and daily_cash_position per account and consolidated in base currency
using the FX rate of that day. Write status changes to the event-history tables.
Add a reconciliation check: opening + sum(ledger) == closing for every account-day.


## Prompt 6: Scenarios and forecast runs

Implement LiquidityScenario (base, delayed-receipt, stress), each defined only by documented parameters (e.g. receipt delay days, collection haircut %, FX shock %, one-off payment multiplier). Implement ForecastRun with versioned runs (FCAST-YYYY-MM-DD-NNN) and a cutoff date, and generate CashFlowForecast items (20,000) from receivables/payables/ledger patterns. A forecast must only use data on or before its cutoff. Store forecast versions without overwriting, and store scenario-run outputs (projected daily liquidity per scenario). Scenarios must differ ONLY through their documented parameters; add a test proving that.

## Prompt 7

Implement actual-versus-forecast comparison: once actuals exist for dates after a run's cutoff, compute variance per forecast item and per day, with an explained variance driver (e.g. delayed receipt, FX move, one-off payment, unforecast item). Produce metrics: MAE, MAPE or WAPE, bias, and forecast accuracy by scenario. Make sure the explanation is derived from the source records, not randomly assigned.

## Prompt 8

Implement an anomaly injector that runs AFTER the valid baseline is generated, with its own seeded RNG stream. Inject these five labelled cases: (1) opening + ledger != closing, (2) FX rate missing, (3) cash flow counted twice, (4) forecast uses actual after cutoff, (5) currency mixed without conversion. Only the targeted rows may change; do not corrupt unrelated rows. Write ground_truth.csv/json (record ID, table, issue_type, description, expected decision) separately. Ensure ground-truth columns never appear in input tables. Add a test that checks this.

## Prompt 9

Build the exception-handling workflow without reading ground_truth: validators that detect each of the five issue types from the data alone, produce an exceptions queue with severity, a reviewer decision field (operations analyst / risk-compliance), and a decision log (append-only). Then write an evaluation script that compares detections against ground_truth.csv and reports precision, recall and F1 per issue type. This shows the exception path and measurable outputs.

## Prompt 10

Write a validation module and pytest tests for: PK uniqueness, FK integrity, allowed enums, date ordering, arithmetic reconciliation, lifecycle transition validity, duplicate checks, and the ID format regexes. Output a validation_summary.json and a readable validation_report.md. Baseline data must pass all checks, and the injected anomalies must be caught only where labelled.

## Prompt 11

Add exports: CSV, JSON and SQL seed files, plus a SQLite DB. Write a script that regenerates the dataset twice with the same seed, then compares row counts and SHA-256 checksums per file, and prints a pass/fail reproducibility report. Also produce a source_manifest (all data is synthetic; no external sources) and record the exact generator command.

## Prompt 12

Act as a strict reviewer using the original problem statement and the evaluation focus: realistic distributions, consistent IDs and joins, lifecycle consistency, correct derived values, edge-case coverage, separate ground truth, reproducibility and documentation. Audit the repo and list concrete gaps, bugs and weak spots, ranked by severity. Do not fix anything yet.

## Prompt 13

Write README.md: problem summary, setup, exact run commands (starter and full scale), how to run tests and validation, repo structure, key assumptions and trade-offs, known limitations, and next steps. Add docs/ai_usage.md with sections: where AI accelerated the work, where I relied heavily on it, and what I personally reviewed, changed and validated. Leave placeholders for my own notes.

## Follow-up requests after Prompt 13

Sent as typed (spelling kept as written).

* give me all the files in one folder im not able to find in the local system
* tell me the excat folder path in my local system where the folder will be
* i have the folder now tell me how to run and excecute the file and later we go with the prompts and fieles they have mentioned in the problem statement
* tell me after runnig all these is my project done whatever they mentioned in the problem statemtn are we getting the results?
* do the things whatever you have not done yet
* tell me how to run the project
* (pasted the output of `python -m pytest -q` from my Windows machine, which showed 2 failures in tests/test_generate_cli.py)
* give me the prompts file like evaluators will check my prompt skillls

## How the prompts were structured

Observations from Prompts 6 to 13, for the reviewer:

* Each prompt names the entities, outputs and tests it needs, and ties them to the problem statement.
* Constraints are stated as rules the output can be checked against: forecasts use only data up to the cutoff, scenarios differ only through documented parameters, the injector runs after a valid baseline and changes only the targeted rows, the detectors never read ground truth.
* Several prompts require a test that proves the rule (scenario isolation, ground-truth columns absent from inputs, baseline passes all checks, reproducibility by checksum).
* Prompt 12 asked for a strict audit with no fixes, so that review and repair were separate steps. Fixes were requested afterwards.
* Prompt 13 asked for placeholders for my own notes, because the AI-use disclosure has to reflect what I personally did.