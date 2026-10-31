# Prompt history

All prompts I sent to the AI tool (Claude, in Claude Code) while building this project. Responses are not included (optional per the FAQ).

<!-- TODO(you): this file is a draft assembled from the session. Prompts 1-5 and my first message (the task text, the guideline PDFs, the request for a step-by-step prompt list) are NOT reproduced here because I could not retrieve their exact wording. Paste them from your own chat history, in order, above Prompt 6, and check the wording of everything below against your chat. Also add any prompts you sent to other AI tools. If a tool cannot export prompts, say so here. -->

## Prompts 1 to 5

<!-- TODO(you): paste exactly as sent. -->

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

* give me all the files in one folder, I'm not able to find them in the local system
* tell me the exact folder path in my local system where the folder will be
* i have the folder now, tell me how to run and execute the file and later we go with the prompts and files they have mentioned in the problem statement
* tell me after running all these is my project done, whatever they mentioned in the problem statement, are we getting the results?
* do the things whatever you have not done yet
