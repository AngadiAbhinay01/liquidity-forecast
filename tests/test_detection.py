"""Detection, exception workflow and evaluation tests."""
import ast
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from src.config import build_config
from src.detect import params as DP
from src.detect import run_detection
from src.detect.__main__ import load_input, main as detect_main
from src.detect.detectors import detect_all
from src.detect.evaluate import evaluate, main as eval_main
from src.detect.workflow import ExceptionWorkflow, role_for, severity_for
from src.generate import main as generate_main
from src.pipeline import generate_all, generate_dataset

ROOT = Path(__file__).resolve().parent.parent
ISSUES = list(DP.BASE_SEVERITY)


@pytest.fixture(scope="module")
def starter():
    cfg = build_config("starter")
    return generate_dataset(cfg)


@pytest.fixture(scope="module")
def detections(starter):
    return detect_all(starter.tables)


def fresh(baseline):
    return {k: v.copy() for k, v in baseline.items()}


def keyset(df):
    return set(zip(df.issue_type, df.table_name, df.record_id))


# ---- accuracy on injected data, silence on clean data -------------------------------------------
def test_every_injected_case_is_found_and_nothing_else(starter, detections):
    gt = starter.ground_truth
    assert keyset(detections) == set(zip(gt.issue_type, gt.table, gt.record_id))


def test_the_clean_baseline_raises_no_exceptions(starter):
    assert len(detect_all(starter.baseline)) == 0


def test_evaluation_is_perfect_on_the_starter_dataset(starter, detections):
    wf = ExceptionWorkflow().open_from(detections)
    res = evaluate(wf.queue, starter.ground_truth)
    for it in ISSUES:
        assert res["per_issue_type"][it]["precision"] == 1.0
        assert res["per_issue_type"][it]["recall"] == 1.0
    assert res["micro"]["f1"] == 1.0 and res["macro"]["f1"] == 1.0
    assert res["routing_accuracy"] == 1.0 and res["recommended_decision_accuracy"] == 1.0


# ---- independence from the injector and the ground truth ------------------------------------------
def test_detection_package_never_mentions_ground_truth():
    banned = {"ground_truth", "anomalies", "inject_anomalies", "expected_decision", "injected_changes"}
    for f in ("detectors", "workflow", "params", "__init__", "__main__"):
        tree = ast.parse((ROOT / "src" / "detect" / f"{f}.py").read_text())
        names = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.ImportFrom):
                names.update((n.module or "").split("."))
                names.update(a.name for a in n.names)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                names.add(n.value)
        assert not (names & banned), (f, names & banned)


def test_only_the_evaluation_module_opens_the_ground_truth_file():
    for p in (ROOT / "src" / "detect").glob("*.py"):
        text = p.read_text()
        if p.name == "evaluate.py":
            assert "ground_truth" in text
        else:
            assert "ground_truth.csv" not in text and "ground_truth/" not in text, p.name


def test_cli_detection_ignores_a_corrupt_or_missing_ground_truth(tmp_path):
    out = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(out)])
    detect_main(["--data", str(out)])
    first = (out / "exceptions" / "exception_queue.csv").read_bytes()
    (out / "ground_truth" / "ground_truth.csv").write_text("garbage\n")
    detect_main(["--data", str(out)])
    assert (out / "exceptions" / "exception_queue.csv").read_bytes() == first
    for f in (out / "ground_truth").iterdir():
        f.unlink()
    (out / "ground_truth").rmdir()
    detect_main(["--data", str(out)])
    assert (out / "exceptions" / "exception_queue.csv").read_bytes() == first


def test_csv_round_trip_gives_the_same_detections_as_memory(tmp_path, starter, detections):
    out = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(out)])
    from_csv = detect_all(load_input(out))
    assert keyset(from_csv) == keyset(detections)


# ---- hand-made anomalies (not made by the injector) and near-misses ---------------------------------
@pytest.fixture(scope="module")
def baseline():
    return generate_all(build_config("starter"))


def test_hand_made_position_break_mid_series_flags_the_row_and_its_chain_successor(baseline):
    t = fresh(baseline)
    p = t["daily_cash_position"].sort_values(["bank_account_id", "position_date"]).reset_index(drop=True)
    t["daily_cash_position"] = p
    i = 10
    assert p.bank_account_id[i] == p.bank_account_id[i + 1]
    p.loc[i, "closing_minor"] += 12345
    d = detect_all(t)
    key = lambda j: f"{p.bank_account_id[j]}|{p.position_date[j]}"
    # the edited row breaks its own arithmetic; the next row's opening no longer matches this closing
    assert set(d.record_id) == {key(i), key(i + 1)}
    assert set(d.issue_type) == {"OPENING_PLUS_LEDGER_NE_CLOSING"}


def test_hand_made_position_break_on_the_last_row_flags_exactly_that_row(baseline):
    t = fresh(baseline)
    p = t["daily_cash_position"].sort_values(["bank_account_id", "position_date"]).reset_index(drop=True)
    t["daily_cash_position"] = p
    i = p.groupby("bank_account_id").tail(1).index[0]
    p.loc[i, "closing_minor"] -= 777
    d = detect_all(t)
    assert list(d.record_id) == [f"{p.bank_account_id[i]}|{p.position_date[i]}"]


def test_hand_made_missing_fx_and_a_base_currency_row_with_null_fx(baseline):
    t = fresh(baseline)
    led = t["ledger_entry"]
    base_row = led[led.currency == "USD"].index[0]
    assert pd.isna(led.loc[base_row, "fx_rate_id"])          # a USD row with NULL fx is normal
    fx_row = led[led.currency != "USD"].index[3]
    led.loc[fx_row, ["fx_rate_id", "base_amount_minor"]] = [None, None]
    d = detect_all(t)
    assert list(d.record_id) == [led.loc[fx_row, "ledger_entry_id"]]
    assert d.issue_type.iloc[0] == "FX_RATE_MISSING"


def test_hand_made_double_count_flags_the_repeat_not_the_original(baseline):
    t = fresh(baseline)
    cf = t["cash_flow_forecast"]
    row = cf[cf.source_type != "RUN_RATE"].iloc[[5]].copy() if "RUN_RATE" in set(cf.source_type) else cf.iloc[[5]].copy()
    original = row.cash_flow_forecast_id.iloc[0]
    row["cash_flow_forecast_id"] = "CF-2026-999999"
    t["cash_flow_forecast"] = pd.concat([cf, row], ignore_index=True)
    d = detect_all(t)
    assert list(d.record_id) == ["CF-2026-999999"] and original not in set(d.record_id)


def test_the_same_item_in_two_different_runs_is_not_a_double_count(baseline):
    # near miss: a re-run (new version at the same cutoff) legitimately repeats items
    assert len(detect_all(baseline)) == 0
    cf = baseline["cash_flow_forecast"]
    key = ["bank_account_id", "source_type", "receivable_id", "payable_id", "expected_date"]
    repeated_across_runs = cf.groupby(key, dropna=False).forecast_run_id.nunique().gt(1).any()
    assert repeated_across_runs, "this check needs the baseline to contain cross-run repeats"


def test_hand_made_cutoff_leak(baseline):
    t = fresh(baseline)
    cf = t["cash_flow_forecast"]
    run = t["forecast_run"].set_index("forecast_run_id")
    i = cf.index[7]
    cf.loc[i, "source_data_date"] = (pd.Timestamp(run.loc[cf.loc[i, "forecast_run_id"], "cutoff_date"])
                                     + pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    d = detect_all(t)
    assert list(d.record_id) == [cf.loc[i, "cash_flow_forecast_id"]]
    assert d.issue_type.iloc[0] == "FORECAST_USES_ACTUAL_AFTER_CUTOFF"


def test_hand_made_currency_mix(baseline):
    t = fresh(baseline)
    led = t["ledger_entry"]
    i = led[led.currency != "USD"].index[0]
    led.loc[i, ["currency", "fx_rate_id", "base_amount_minor"]] = ["USD", None, led.loc[i, "amount_minor"]]
    d = detect_all(t)
    assert list(d.record_id) == [led.loc[i, "ledger_entry_id"]]
    assert d.issue_type.iloc[0] == "CURRENCY_MIXED_WITHOUT_CONVERSION"


def test_detections_have_evidence_and_are_sorted(detections):
    assert detections.evidence.str.len().gt(10).all()
    assert detections.detected_by.notna().all()
    order = detections.sort_values(["issue_type", "table_name", "record_id"], kind="stable").index
    assert list(order) == list(detections.index)


# ---- queue, severity, routing ------------------------------------------------------------------
def test_queue_has_one_row_per_detection_with_playbook_fields(starter, detections):
    q = run_detection(starter.tables).queue
    assert len(q) == len(detections) and q.exception_id.is_unique
    assert q.exception_id.str.fullmatch(r"EXC-\d{6}").all()
    assert (q.status == "OPEN").all() and q.reviewer_decision.isna().all()
    assert (q.recommended_decision == q.issue_type.map(DP.RECOMMENDED_DECISION)).all()
    assert set(q.severity) <= set(DP.SEVERITY_ORDER)


def test_queue_is_ordered_most_severe_first(starter):
    q = run_detection(starter.tables).queue
    rank = q.severity.map(DP.SEVERITY_ORDER.index)
    assert rank.is_monotonic_decreasing


def test_severity_levels_and_the_size_bump():
    assert severity_for("FX_RATE_MISSING", 10_000) == "MEDIUM"
    assert severity_for("FX_RATE_MISSING", None) == "MEDIUM"
    assert severity_for("FX_RATE_MISSING", 100_000 * 100) == "HIGH"          # exactly at the threshold
    assert severity_for("FX_RATE_MISSING", 100_000 * 100 - 1) == "MEDIUM"
    assert severity_for("DOUBLE_COUNTED_CASH_FLOW", 10**12) == "HIGH"        # capped, not CRITICAL
    assert severity_for("FORECAST_USES_ACTUAL_AFTER_CUTOFF", 1) == "CRITICAL"


def test_routing_follows_the_documented_rule():
    assert role_for("FORECAST_USES_ACTUAL_AFTER_CUTOFF", "CRITICAL") == "RISK_COMPLIANCE_REVIEWER"
    assert role_for("CURRENCY_MIXED_WITHOUT_CONVERSION", "HIGH") == "RISK_COMPLIANCE_REVIEWER"
    assert role_for("OPENING_PLUS_LEDGER_NE_CLOSING", "HIGH") == "RISK_COMPLIANCE_REVIEWER"
    assert role_for("FX_RATE_MISSING", "MEDIUM") == "OPERATIONS_ANALYST"
    assert role_for("FX_RATE_MISSING", "HIGH") == "OPERATIONS_ANALYST"
    assert role_for("DOUBLE_COUNTED_CASH_FLOW", "HIGH") == "OPERATIONS_ANALYST"


# ---- decision log and authority --------------------------------------------------------------------
@pytest.fixture()
def wf(starter):
    return run_detection(starter.tables)


def pick(wf, role):
    q = wf.queue
    return q[q.assigned_role == role].exception_id.iloc[0]


def test_each_exception_starts_with_detected_then_routed(wf):
    log = wf.log
    for eid, g in log.groupby("exception_id"):
        assert list(g.event_type) == ["DETECTED", "ROUTED"] and list(g.event_seq) == [1, 2]
    assert list(log.log_id) == list(range(1, len(log) + 1))


def test_operations_analyst_decides_own_items_and_the_log_records_it(wf):
    e = pick(wf, "OPERATIONS_ANALYST")
    rec = wf.queue.set_index("exception_id").loc[e, "recommended_decision"]
    wf.record_decision(e, "OPERATIONS_ANALYST", rec, "checked")
    row = wf.queue.set_index("exception_id").loc[e]
    assert row.status == "DECIDED" and row.reviewer_decision == rec and row.decided_by_role == "OPERATIONS_ANALYST"
    last = wf.log[wf.log.exception_id == e].iloc[-1]
    assert last.event_type == "DECISION_RECORDED" and last.decision == rec and last.from_status == "OPEN"


def test_analyst_cannot_decide_risk_items_but_can_escalate_own(wf):
    r = pick(wf, "RISK_COMPLIANCE_REVIEWER")
    with pytest.raises(PermissionError):
        wf.record_decision(r, "OPERATIONS_ANALYST", DP.RECOMMENDED_DECISION["FORECAST_USES_ACTUAL_AFTER_CUTOFF"])
    a = pick(wf, "OPERATIONS_ANALYST")
    wf.escalate(a, "OPERATIONS_ANALYST", "unclear")
    row = wf.queue.set_index("exception_id").loc[a]
    assert row.status == "ESCALATED" and row.assigned_role == "RISK_COMPLIANCE_REVIEWER"
    wf.record_decision(a, "RISK_COMPLIANCE_REVIEWER", DP.DISMISS_DECISION, "false alarm")
    assert wf.queue.set_index("exception_id").loc[a, "status"] == "DECIDED"


def test_only_risk_compliance_may_dismiss(wf):
    a = pick(wf, "OPERATIONS_ANALYST")
    with pytest.raises(PermissionError):
        wf.record_decision(a, "OPERATIONS_ANALYST", DP.DISMISS_DECISION)
    wf.record_decision(a, "RISK_COMPLIANCE_REVIEWER", DP.DISMISS_DECISION)


def test_one_decision_per_exception_and_no_escalating_decided_or_risk_items(wf):
    a = pick(wf, "OPERATIONS_ANALYST")
    rec = wf.queue.set_index("exception_id").loc[a, "recommended_decision"]
    wf.record_decision(a, "OPERATIONS_ANALYST", rec)
    with pytest.raises(ValueError):
        wf.record_decision(a, "RISK_COMPLIANCE_REVIEWER", rec)
    with pytest.raises(ValueError):
        wf.escalate(a, "OPERATIONS_ANALYST")
    with pytest.raises(ValueError):
        wf.escalate(pick(wf, "RISK_COMPLIANCE_REVIEWER"), "OPERATIONS_ANALYST")


def test_bad_inputs_are_rejected_and_leave_the_log_unchanged(wf):
    n = len(wf.log)
    a = pick(wf, "OPERATIONS_ANALYST")
    for bad in (lambda: wf.record_decision(a, "CEO", "REMOVE_DUPLICATE_ITEM"),
                lambda: wf.record_decision(a, "OPERATIONS_ANALYST", "SHRUG"),
                lambda: wf.record_decision("EXC-999999", "OPERATIONS_ANALYST", "REMOVE_DUPLICATE_ITEM"),
                lambda: wf.escalate("EXC-999999", "OPERATIONS_ANALYST")):
        with pytest.raises((ValueError, KeyError, PermissionError)):
            bad()
    assert len(wf.log) == n


def test_log_is_append_only_in_memory(wf):
    before = wf.log.copy()
    view = wf.log                      # a copy: editing it must not reach the workflow
    view.loc[view.index[0], "log_id"] = 999
    view.drop(view.index, inplace=True)
    pd.testing.assert_frame_equal(wf.log, before)
    a = pick(wf, "OPERATIONS_ANALYST")
    wf.record_decision(a, "OPERATIONS_ANALYST", wf.queue.set_index("exception_id").loc[a, "recommended_decision"])
    after = wf.log
    assert after.iloc[:len(before)].to_csv(index=False) == before.to_csv(index=False)     # earlier rows untouched
    assert len(after) == len(before) + 1


def test_the_shipped_queue_has_no_decisions_yet(tmp_path):
    out = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(out)])
    detect_main(["--data", str(out)])
    q = pd.read_csv(out / "exceptions" / "exception_queue.csv")
    assert (q.status == "OPEN").all() and q.reviewer_decision.isna().all()
    log = pd.read_csv(out / "exceptions" / "decision_log.csv")
    assert set(log.event_type) == {"DETECTED", "ROUTED"}
    summary = json.loads((out / "exceptions" / "exception_summary.json").read_text())
    assert summary["exceptions"] == len(q)


# ---- SQLite version of the queue and log ---------------------------------------------------------------
def load_sql(wf):
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((ROOT / "sql" / "exceptions.sql").read_text())
    for name, df in (("exception_queue", wf.queue), ("decision_log", wf.log)):
        cols = list(df.columns)
        rows = [tuple(None if pd.isna(v) else (v.item() if hasattr(v, "item") else v) for v in r)
                for r in df.itertuples(index=False)]
        con.executemany(f"INSERT INTO {name} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", rows)
    return con


def test_sql_tables_accept_the_workflow_output_including_decisions(wf):
    a = pick(wf, "OPERATIONS_ANALYST")
    wf.record_decision(a, "OPERATIONS_ANALYST", wf.queue.set_index("exception_id").loc[a, "recommended_decision"], "ok")
    con = load_sql(wf)
    assert con.execute("SELECT COUNT(*) FROM exception_queue").fetchone()[0] == len(wf.queue)
    assert con.execute("SELECT COUNT(*) FROM decision_log").fetchone()[0] == len(wf.log)


def test_sql_decision_log_rejects_update_and_delete(wf):
    con = load_sql(wf)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("UPDATE decision_log SET note = 'x' WHERE log_id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        con.execute("DELETE FROM decision_log WHERE log_id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("DELETE FROM exception_queue WHERE exception_id = 'EXC-000001'")


def test_sql_queue_freezes_identity_fields_and_decided_rows(wf):
    con = load_sql(wf)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE exception_queue SET severity = 'LOW' WHERE exception_id = 'EXC-000001'")
    con.execute("UPDATE exception_queue SET status='DECIDED', reviewer_decision='REMOVE_DUPLICATE_ITEM', "
                "decided_by_role='OPERATIONS_ANALYST' WHERE exception_id = 'EXC-000001'")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE exception_queue SET decision_note = 'later' WHERE exception_id = 'EXC-000001'")


def test_sql_rejects_a_decided_row_without_a_decision(wf):
    con = load_sql(wf)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("UPDATE exception_queue SET status = 'DECIDED' WHERE exception_id = 'EXC-000001'")


# ---- evaluation arithmetic ---------------------------------------------------------------------------------
def make_truth(rows):
    return pd.DataFrame(rows, columns=["issue_type", "table", "record_id", "expected_decision", "expected_reviewer_role"])


def make_queue(rows):
    return pd.DataFrame(rows, columns=["issue_type", "table_name", "record_id", "assigned_role",
                                       "recommended_decision", "severity"])


def test_evaluation_counts_tp_fp_fn_per_type_with_known_answers():
    ft = "FX_RATE_MISSING"
    dc = "DOUBLE_COUNTED_CASH_FLOW"
    truth = make_truth([(ft, "ledger_entry", f"L{i}", "D", "OPERATIONS_ANALYST") for i in range(4)]
                       + [(dc, "cash_flow_forecast", "C1", "D", "OPERATIONS_ANALYST")])
    queue = make_queue([(ft, "ledger_entry", "L0", "OPERATIONS_ANALYST", "D", "MEDIUM"),
                        (ft, "ledger_entry", "L1", "RISK_COMPLIANCE_REVIEWER", "X", "MEDIUM"),
                        (ft, "ledger_entry", "L9", "OPERATIONS_ANALYST", "D", "MEDIUM"),       # false positive
                        (dc, "cash_flow_forecast", "C1", "OPERATIONS_ANALYST", "D", "HIGH")])
    r = evaluate(queue, truth)
    f = r["per_issue_type"][ft]
    assert (f["tp"], f["fp"], f["fn"]) == (2, 1, 2)
    assert f["precision"] == pytest.approx(2 / 3) and f["recall"] == pytest.approx(0.5)
    assert f["f1"] == pytest.approx(2 * (2 / 3) * 0.5 / (2 / 3 + 0.5))
    assert r["per_issue_type"][dc]["f1"] == 1.0
    assert r["per_issue_type"]["FORECAST_USES_ACTUAL_AFTER_CUTOFF"]["precision"] is None   # nothing detected, nothing injected
    assert (r["micro"]["tp"], r["micro"]["fp"], r["micro"]["fn"]) == (3, 1, 2)
    assert r["routing_accuracy"] == pytest.approx(2 / 3)
    assert r["recommended_decision_accuracy"] == pytest.approx(2 / 3)
    assert [m["record_id"] for m in r["false_positives"]] == ["L9"]
    assert sorted(m["record_id"] for m in r["missed"]) == ["L2", "L3"]


def test_matching_requires_the_table_too():
    truth = make_truth([("FX_RATE_MISSING", "ledger_entry", "L0", "D", "OPERATIONS_ANALYST")])
    queue = make_queue([("FX_RATE_MISSING", "cash_flow_forecast", "L0", "OPERATIONS_ANALYST", "D", "MEDIUM")])
    r = evaluate(queue, truth)
    assert r["micro"]["tp"] == 0 and r["micro"]["fp"] == 1 and r["micro"]["fn"] == 1


def test_a_degraded_queue_lowers_recall(starter):
    q = run_detection(starter.tables).queue
    r = evaluate(q.iloc[1:], starter.ground_truth)
    assert r["micro"]["fn"] == 1 and r["micro"]["recall"] < 1.0 and r["micro"]["precision"] == 1.0


def test_evaluate_cli_writes_reports(tmp_path):
    out = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(out)])
    detect_main(["--data", str(out)])
    eval_main(["--data", str(out)])
    res = json.loads((out / "evaluation" / "detection_evaluation.json").read_text())
    assert res["micro"]["f1"] == 1.0
    md = (out / "evaluation" / "detection_evaluation.md").read_text()
    assert all(it in md for it in ISSUES)


def test_detection_parameters_are_documented():
    doc = (ROOT / "docs" / "distributions.md").read_text()
    missing = [n for n in vars(DP) if n.isupper() and n not in doc]
    assert not missing, missing
