"""Turn check outcomes into validation_summary.json and validation_report.md."""
from __future__ import annotations

import pandas as pd

from .checks import CATEGORIES, VIOLATION_COLUMNS, CheckOutcome, run_checks

SAMPLE_PER_CHECK = 5


def validate(tables: dict[str, pd.DataFrame]) -> list[CheckOutcome]:
    return run_checks(tables)


def all_violations(outcomes: list[CheckOutcome]) -> pd.DataFrame:
    parts = [o.violations for o in outcomes if len(o.violations)]
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=VIOLATION_COLUMNS)
    return out.sort_values(["check_id", "table_name", "record_id"], kind="stable").reset_index(drop=True)


def summary(outcomes: list[CheckOutcome], tables: dict[str, pd.DataFrame], dataset: str = "") -> dict:
    v = all_violations(outcomes)
    cats = {}
    for c in CATEGORIES:
        o = [x for x in outcomes if x.category == c]
        cats[c] = {"checks": len(o), "rows_examined": int(sum(x.n_checked for x in o)),
                   "violations": int(sum(x.n_violations for x in o)),
                   "status": "PASS" if sum(x.n_violations for x in o) == 0 else "FAIL"}
    return {
        "dataset": dataset,
        "status": "PASS" if v.empty else "FAIL",
        "checks_run": len(outcomes),
        "checks_failed": int(sum(1 for x in outcomes if x.n_violations)),
        "violations": int(len(v)),
        "distinct_records_flagged": int(v[["table_name", "record_id"]].drop_duplicates().shape[0]),
        "row_counts": {k: int(len(df)) for k, df in sorted(tables.items())},
        "by_category": cats,
        "checks": [{"check_id": x.check_id, "category": x.category, "description": x.description,
                    "rows_examined": x.n_checked, "violations": x.n_violations,
                    "status": "PASS" if x.n_violations == 0 else "FAIL"} for x in outcomes],
        "violations_by_table": {k: int(n) for k, n in v.groupby("table_name").size().sort_index().items()},
    }


def report_markdown(s: dict, outcomes: list[CheckOutcome], label_report: dict | None = None) -> str:
    L = [f"# Validation report{': ' + s['dataset'] if s['dataset'] else ''}", "",
         f"**Overall: {s['status']}.** {s['checks_run']} checks run, {s['checks_failed']} with violations, "
         f"{s['violations']} violations on {s['distinct_records_flagged']} distinct records.", "",
         "## By category", "", "| Category | Checks | Rows examined | Violations | Status |", "|---|---|---|---|---|"]
    for c, d in s["by_category"].items():
        L.append(f"| {c} | {d['checks']} | {d['rows_examined']:,} | {d['violations']} | {d['status']} |")
    L += ["", "## Checks", "", "| Check | Category | Rows examined | Violations | What it verifies |", "|---|---|---|---|---|"]
    for c in s["checks"]:
        L.append(f"| `{c['check_id']}` | {c['category']} | {c['rows_examined']:,} | {c['violations']} | {c['description']} |")
    failing = [o for o in outcomes if o.n_violations]
    if failing:
        L += ["", "## Violations (first rows of each failing check)", ""]
        for o in failing:
            L += [f"### `{o.check_id}` ({o.n_violations})", "", "| Table | Record | Detail |", "|---|---|---|"]
            for r in o.violations.head(SAMPLE_PER_CHECK).itertuples(index=False):
                L.append(f"| {r.table_name} | `{r.record_id}` | {str(r.detail).replace('|', '/')} |")
            L.append("")
    if label_report is not None:
        L += ["## Comparison with the labelled anomalies", "",
              f"Labelled cases: {label_report['labelled_cases']}. Caught by their expected check: "
              f"{label_report['caught_by_expected_check']}. Caught by any check: {label_report['caught_by_any_check']}. "
              f"Flagged records that are not labelled: **{len(label_report['unlabelled_flags'])}**.", "",
              "| Issue type | Cases | Caught | Expected check |", "|---|---|---|---|"]
        for k, d in label_report["by_issue_type"].items():
            L.append(f"| {k} | {d['cases']} | {d['caught_by_expected_check']} | `{d['expected_check']}` |")
        if label_report["unlabelled_flags"]:
            L += ["", "Unlabelled flags:"] + [f"- {r['check_id']} {r['table_name']} {r['record_id']}"
                                              for r in label_report["unlabelled_flags"]]
        L.append("")
    elif s["status"] == "PASS":
        L += ["", "Every check passed: this dataset is valid.", ""]
    return "\n".join(L)
