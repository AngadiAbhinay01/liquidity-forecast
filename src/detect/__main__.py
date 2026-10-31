"""python -m src.detect --data data/   ->  data/exceptions/{exception_queue,decision_log}.csv, summary.json

Reads only <data>/input/*.csv. The ground_truth directory is never opened here.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ..export import write_csv_tables
from . import run_detection

INPUT_TABLES = ("currency", "currency_rate", "legal_entity", "bank_account", "receivable", "payable",
                "ledger_entry", "settlement_link", "daily_cash_position", "liquidity_scenario", "forecast_run",
                "cash_flow_forecast")


def load_input(data_dir: Path) -> dict[str, pd.DataFrame]:
    return {name: pd.read_csv(data_dir / "input" / f"{name}.csv", low_memory=False) for name in INPUT_TABLES}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.detect", description="Detect exceptions in a generated dataset.")
    p.add_argument("--data", type=Path, default=Path("data"))
    args = p.parse_args(argv)
    wf = run_detection(load_input(args.data))
    out = args.data / "exceptions"
    write_csv_tables({"exception_queue": wf.queue, "decision_log": wf.log}, out)
    q = wf.queue
    summary = {"exceptions": int(len(q)),
               "by_issue_type": q.issue_type.value_counts().sort_index().to_dict(),
               "by_severity": q.severity.value_counts().to_dict(),
               "by_assigned_role": q.assigned_role.value_counts().to_dict(),
               "status": q.status.value_counts().to_dict()}
    (out / "exception_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(q)} exceptions -> {out}")
    for k, v in summary["by_issue_type"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
