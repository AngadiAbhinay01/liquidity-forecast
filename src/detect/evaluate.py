"""python -m src.detect.evaluate --data data/

Compares the exception queue with ground_truth.csv and reports precision, recall and F1 per
issue type. This is the ONLY module in the package that opens the ground truth, and it
runs after detection.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from ..anomalies.params import ISSUE_TYPES


def _prf(tp: int, fp: int, fn: int) -> dict:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p and r else (0.0 if p is not None and r is not None else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def evaluate(queue: pd.DataFrame, truth: pd.DataFrame) -> dict:
    """Match on (issue_type, table, record_id)."""
    key = ["issue_type", "table_name", "record_id"]
    det = queue.rename(columns={})[key + ["assigned_role", "recommended_decision", "severity"]]
    gt = truth.rename(columns={"table": "table_name"})[key + ["expected_decision", "expected_reviewer_role"]]
    both = det.merge(gt, on=key, how="outer", indicator=True)
    per_type = {}
    for it in ISSUE_TYPES:
        b = both[both.issue_type == it]
        per_type[it] = _prf(int((b._merge == "both").sum()), int((b._merge == "left_only").sum()),
                            int((b._merge == "right_only").sum()))
    tp, fp, fn = (sum(v[k] for v in per_type.values()) for k in ("tp", "fp", "fn"))
    micro = _prf(tp, fp, fn)
    f1s = [v["f1"] for v in per_type.values() if v["f1"] is not None]
    macro = {"f1": sum(f1s) / len(f1s) if f1s else None,
             "precision": _mean([v["precision"] for v in per_type.values()]),
             "recall": _mean([v["recall"] for v in per_type.values()])}
    matched = both[both._merge == "both"]
    return {
        "per_issue_type": per_type, "micro": micro, "macro": macro,
        "detections": int(len(det)), "ground_truth_cases": int(len(gt)),
        "routing_accuracy": float((matched.assigned_role == matched.expected_reviewer_role).mean()) if len(matched) else None,
        "recommended_decision_accuracy": float((matched.recommended_decision == matched.expected_decision).mean())
        if len(matched) else None,
        "false_positives": both[both._merge == "left_only"][key].to_dict(orient="records"),
        "missed": both[both._merge == "right_only"][key].to_dict(orient="records"),
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def report_markdown(res: dict) -> str:
    def f(x):
        return "n/a" if x is None else f"{x:.3f}"
    lines = ["# Detection evaluation", "",
             f"Detections: {res['detections']}; ground-truth cases: {res['ground_truth_cases']}.", "",
             "| Issue type | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|"]
    for k, v in res["per_issue_type"].items():
        lines.append(f"| {k} | {v['tp']} | {v['fp']} | {v['fn']} | {f(v['precision'])} | {f(v['recall'])} | {f(v['f1'])} |")
    m = res["micro"]
    lines.append(f"| **micro (all types)** | {m['tp']} | {m['fp']} | {m['fn']} | {f(m['precision'])} | {f(m['recall'])} | {f(m['f1'])} |")
    lines += ["", f"Macro F1: {f(res['macro']['f1'])}. Routing accuracy on matched cases: {f(res['routing_accuracy'])}. "
              f"Recommended-decision accuracy on matched cases: {f(res['recommended_decision_accuracy'])}.", ""]
    if res["false_positives"]:
        lines += ["False positives:"] + [f"- {r['issue_type']} {r['record_id']}" for r in res["false_positives"]] + [""]
    if res["missed"]:
        lines += ["Missed:"] + [f"- {r['issue_type']} {r['record_id']}" for r in res["missed"]] + [""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.detect.evaluate")
    p.add_argument("--data", type=Path, default=Path("data"))
    args = p.parse_args(argv)
    queue = pd.read_csv(args.data / "exceptions" / "exception_queue.csv")
    truth = pd.read_csv(args.data / "ground_truth" / "ground_truth.csv")
    res = evaluate(queue, truth)
    out = args.data / "evaluation"
    out.mkdir(parents=True, exist_ok=True)
    (out / "detection_evaluation.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "detection_evaluation.md").write_text(report_markdown(res) + "\n", encoding="utf-8")
    print(report_markdown(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
