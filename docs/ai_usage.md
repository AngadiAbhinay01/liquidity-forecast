# AI usage

I used Claude (Claude Code) for this project. I gave it the task text and the submission guidelines, then worked through a series of prompts, one stage at a time. The prompts I sent are in `prompts/prompt_history.md`.

Most of the code was written by the AI, and I want to be clear about that.

## Where AI accelerated the work

* **Generator and stages.** Entities, bank accounts, FX rates, transactions (invoices, settlements, reversals), forecast scenarios and runs, variance and accuracy metrics, and the anomaly injector.
* **Tests.** About 600 tests, including hand-built broken inputs for each validation rule, and checks where the code is broken on purpose to confirm a test fails.
* **Validation, detection and exceptions.** The validation checks, the five detectors, the exception queue, the append-only decision log, and the precision / recall / F1 evaluation.
* **Exports and tooling.** CSV, JSON, SQL seed and SQLite writers, the source manifest, and the script that regenerates the data twice and compares checksums.
* **Documentation.** Data dictionary, distributions, design decisions, validation and export notes, the audit, and the README.
* **Finding its own bugs.** The reproducibility script showed that the generator gave different data in different processes, because set order leaked into random draws. This was fixed and a regression test added.

## Where I relied on it most

* The implementation code across all stages, written to the specifications in my prompts.
* The statistical settings behind the "realistic" distributions (log-normal amounts, lateness probabilities by risk tier, settlement shares, scenario parameter values). These are documented in `docs/distributions.md` and are not calibrated against real treasury data.
* The detection and validation rules. They were written by the same assistant as the anomaly injector, so the detection scores are partly circular (see `docs/review/audit_round1.md`, item M3).

## What I did

* **Specified the work.** I wrote the prompts, in order, from the problem statement and the submission guidelines. Each prompt states the entities, outputs, rules and tests required, for example: forecasts use only data up to the cutoff, scenarios differ only through documented parameters, the injector changes only the targeted rows, and the detectors never read the ground truth.
* **Required proof, not just code.** My prompts asked for tests that prove each rule, a validation module with a report, a reproducibility check with SHA-256 checksums, and a separate ground truth.
* **Ordered a strict audit before finishing.** Prompt 12 asked for a severity-ranked review of the repo against the problem statement, with no fixes. I then had the open issues fixed (forecast row count, suspended entities that kept trading, scarcity of large one-off payments). The audit and what remains are in `docs/review/audit_round1.md`.
* **Ran the project on my own machine.** On Windows with a fresh virtual environment the full suite gave 599 passed and 2 failed. I reported the output, the cause was a Windows path issue in a test helper, and it was fixed.
* **Checked the result against the brief.** I asked for the project to be compared with the problem statement and for anything not yet done to be completed.
* **Published and documented it.** I pushed the repository to GitHub, checked the commit history there, and finished the README, prompt history and this file.

## Scope of my verification

I verified the project by running it and its tests, and by checking the outputs against the problem statement. I did not review the code line by line or calibrate the distributions against real treasury data. The known limitations are listed in the README, and the checks anyone can repeat are below.

## Evidence you can repeat

| Claim | How to repeat it |
|---|---|
| Same seed gives identical files across processes | `python -m src.reproduce --scale full` |
| Valid baseline passes every check; anomalies flagged only where labelled | `python -m src.validate --data data/full --compare-ground-truth` |
| Detector precision / recall / F1 per issue type | `python -m src.detect --data data/full` then `python -m src.detect.evaluate --data data/full` |
| Ground truth is absent from inputs | `python -m pytest tests/test_anomalies.py tests/test_detection.py -q` |
| Whole suite | `python -m pytest -q` (about 600 tests; 4 to 20 minutes depending on the machine) |
