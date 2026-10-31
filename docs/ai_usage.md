# AI usage

<!-- TODO(you): this file is a draft. The first two sections were written from the assistant's side of the work and should be corrected to match what actually happened. The third section is yours alone: only you know what you read, changed and checked. -->

Tool used: Claude (Claude Code) working in a session on this repository, one prompt at a time (the prompts are in `prompts/`; <!-- TODO(you): copy your prompt history there. -->).

## Where AI accelerated the work



* **Writing the generator and its stages.** Entity, account and FX generation; transaction planning (invoices, settlement tranches, batching, reversals); forecast scenarios and runs; the variance and metrics code; the anomaly injector.
* **Tests.** Most of the about 600 tests, including hand-built broken inputs for each validation rule, and mutation checks (breaking the code on purpose to confirm a test fails).
* **Validation, detection and exception workflow.** The 30 validation checks, the five detectors, the exception queue and append-only decision log (Python and SQL), the precision / recall / F1 evaluation.
* **Exports and tooling.** CSV / JSON / SQL seed / SQLite writers, the manifest, the reproducibility script.
* **Documentation.** Data dictionary, distributions, design-decision log, validation and export docs, the audit, this README.
* **Finding its own mistakes.** Bugs that surfaced during the work are recorded in the review notes. The most important: the reproducibility script showed that the generator gave different data in different processes (set iteration order leaking into random draws), which in-process tests had missed; it was fixed and a cross-process regression test was added.

## Where I relied heavily on it


* The statistical choices behind the "realistic" distributions (log-normal amounts, lateness probabilities by risk tier, settlement shares, scenario parameter values). I accepted them without independent calibration against real treasury data.
* The modelling interpretation of the brief: ID formats, status lifecycles and their allowed transitions, the list of variance drivers, which anomalies target which rows.
* The detection and validation rules, which the same assistant wrote after writing the injector; the scores they give are therefore partly circular (see `docs/review/audit_round1.md`, M3).
* Long stretches of code that I did not read line by line.



## What I personally reviewed, changed and validated

* Which files or modules I read line by line, and which I only ran.
* Decisions I made or overruled (for example: confirming or changing the account ID format, the scale numbers, which anomalies to inject).
* Changes I made to the assistant's code or docs, and why.
* Checks I ran myself: the commands I executed, the numbers I saw, anything I compared against the problem statement or FAQ.
* Anything I found wrong that the assistant had called finished.
* What I would not trust without more checking.
-->

### Evidence available to verify against

The assistant's own record of checks, so you can repeat them rather than take them on trust:

| Claim | How to repeat it |
|---|---|
| Same seed gives identical files across processes | `python -m src.reproduce --scale full` |
| Valid baseline passes every check; anomalies flagged only where labelled | `python -m src.validate --data data/full --compare-ground-truth` |
| Detector precision / recall / F1 per issue type | `python -m src.detect --data data/full && python -m src.detect.evaluate --data data/full` |
| Ground truth is absent from inputs | `python -m pytest tests/test_anomalies.py tests/test_detection.py -q` |
| Whole suite | `python -m pytest -q` (about 600 tests, about 4 minutes) |
