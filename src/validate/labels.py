"""Compare validation output with the ground truth.

This is the only module in src/validate that touches ground truth, and it runs after the
checks. It answers two questions: does each labelled anomaly trip its expected check, and
does the validator flag anything that is not labelled?
"""
from __future__ import annotations

import pandas as pd

# which check is designed to catch each issue type (other checks may also flag the same record)
EXPECTED_CHECK = {
    "OPENING_PLUS_LEDGER_NE_CLOSING": "POSITION_RECONCILES",
    "FX_RATE_MISSING": "FX_COMPLETE",
    "DOUBLE_COUNTED_CASH_FLOW": "DUP_FORECAST_ITEM",
    "FORECAST_USES_ACTUAL_AFTER_CUTOFF": "DATE_CUTOFF",
    "CURRENCY_MIXED_WITHOUT_CONVERSION": "CURRENCY_ACCOUNT",
}


def compare(violations: pd.DataFrame, truth: pd.DataFrame) -> dict:
    flagged = violations[["check_id", "table_name", "record_id"]].drop_duplicates()
    by_record = flagged.groupby(["table_name", "record_id"]).check_id.apply(set).to_dict()
    truth_keys = set(zip(truth["table"], truth["record_id"]))
    by_type, caught_exp, caught_any = {}, 0, 0
    for it, grp in truth.groupby("issue_type"):
        exp = EXPECTED_CHECK[it]
        n_exp = sum(exp in by_record.get((t, r), set()) for t, r in zip(grp["table"], grp["record_id"]))
        n_any = sum(bool(by_record.get((t, r))) for t, r in zip(grp["table"], grp["record_id"]))
        by_type[it] = {"cases": int(len(grp)), "caught_by_expected_check": int(n_exp), "caught_by_any_check": int(n_any),
                       "expected_check": exp}
        caught_exp += n_exp
        caught_any += n_any
    extra = [{"check_id": c, "table_name": t, "record_id": r} for (t, r), cs in sorted(by_record.items())
             if (t, r) not in truth_keys for c in sorted(cs)]
    return {"labelled_cases": int(len(truth)), "caught_by_expected_check": caught_exp, "caught_by_any_check": caught_any,
            "by_issue_type": dict(sorted(by_type.items())), "unlabelled_flags": extra}
