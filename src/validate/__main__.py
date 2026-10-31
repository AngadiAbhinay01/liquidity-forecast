"""python -m src.validate --data data/ [--compare-ground-truth]

Reads <data>/input/*.csv, runs every check and writes <data>/validation/validation_summary.json,
validation_report.md and validation_violations.csv. Exit status 0 when every check passes,
1 when any record is flagged (so a shipped dataset with injected anomalies exits 1 by design).
With --compare-ground-truth the labelled cases are read afterwards and compared.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ..detect.__main__ import INPUT_TABLES
from . import all_violations, report_markdown, summary, validate

ALL_TABLES = INPUT_TABLES + ("receivable_status_event", "payable_status_event", "forecast_run_status_event",
                             "forecast_daily_projection", "forecast_vs_actual")


def load_all(data_dir: Path) -> dict[str, pd.DataFrame]:
    return {n: pd.read_csv(data_dir / "input" / f"{n}.csv", low_memory=False) for n in ALL_TABLES}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.validate")
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--compare-ground-truth", action="store_true")
    args = p.parse_args(argv)
    tables = load_all(args.data)
    outcomes = validate(tables)
    s = summary(outcomes, tables, dataset=str(args.data.name))
    viol = all_violations(outcomes)
    label_report = None
    if args.compare_ground_truth:
        from .labels import compare
        label_report = compare(viol, pd.read_csv(args.data / "ground_truth" / "ground_truth.csv"))
        s["ground_truth_comparison"] = label_report
    out = args.data / "validation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "validation_summary.json").write_text(json.dumps(s, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "validation_report.md").write_text(report_markdown(s, outcomes, label_report) + "\n", encoding="utf-8")
    viol.to_csv(out / "validation_violations.csv", index=False, lineterminator="\n")
    print(f"{s['status']}: {s['checks_run']} checks, {s['violations']} violations on "
          f"{s['distinct_records_flagged']} records -> {out}")
    return 0 if s["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
