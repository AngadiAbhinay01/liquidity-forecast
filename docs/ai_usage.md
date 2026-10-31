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

## Where I relied heavily on it

* The statistical choices behind the "realistic" distributions (log-normal amounts, lateness probabilities by risk tier, settlement shares, scenario parameter values). They were not calibrated against real treasury data.
* The reading of the brief into ID formats, status lifecycles, allowed transitions, variance drivers, and which rows each anomaly targets.
* The detection and validation rules. The same assistant wrote them after the injector, so the detection scores are partly circular (see `docs/review/audit_round1.md`, item M3).
* Most of the code. I did not read it line by line.

## What I personally did

* **Directed the work.** I gave the task and guidelines and sent the prompts in order. Prompt 12 asked for a strict audit with no fixes, then I asked for the open problems to be fixed.
* **Ran the test suite on my own machine (Windows).** The first full run gave 599 passed and 2 failed. I reported the failure output. The cause was Windows path separators in a test helper, and the test was fixed. The data generation itself was not affected.
* **Asked for the project to be checked against the problem statement**, and for the list of what was still not done.
* **Know the limits.** The README lists them: no payroll, tax or holiday cycles, many overdrawn accounts, modest forecast accuracy, and detection scores that depend on the same author writing the injector and the detectors.

### Checks I ran myself before submitting

<!-- Fill in only the lines you really did, with the real result. Delete the rest. -->

* [ ] `python -m pytest tests/test_generate_cli.py -q`: result: ____
* [ ] Starter run from the README: result: ____
* [ ] `python -m src.validate --data data/starter`: result: ____
* [ ] Read the README and the problem statement side by side and compared them: ____
* [ ] Something I changed or fixed myself: ____

## Evidence you can repeat

| Claim | How to repeat it |
|---|---|
| Same seed gives identical files across processes | `python -m src.reproduce --scale full` |
| Valid baseline passes every check; anomalies flagged only where labelled | `python -m src.validate --data data/full --compare-ground-truth` |
| Detector precision / recall / F1 per issue type | `python -m src.detect --data data/full` then `python -m src.detect.evaluate --data data/full` |
| Ground truth is absent from inputs | `python -m pytest tests/test_anomalies.py tests/test_detection.py -q` |
| Whole suite | `python -m pytest -q` (about 600 tests; 4 to 20 minutes depending on the machine) |
