"""Anomaly injection: only labelled rows change, ground truth stays out of the inputs."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.anomalies import GROUND_TRUTH_COLUMNS, cases_per_type, inject_anomalies
from src.anomalies import params as P
from src.config import build_config
from src.db import create_database, load_tables
from src.generate import main
from src.pipeline import generate_all, generate_dataset
from src.rng import RngStreams

ROOT = Path(__file__).resolve().parent.parent
PKS = {"daily_cash_position": ["bank_account_id", "position_date"], "ledger_entry": ["ledger_entry_id"],
       "cash_flow_forecast": ["cash_flow_forecast_id"]}


@pytest.fixture(scope="module", params=["starter", "full"])
def ds(request):
    cfg = build_config(request.param)
    return cfg, generate_dataset(cfg)


@pytest.fixture(scope="module")
def starter_ds():
    cfg = build_config("starter")
    return cfg, generate_dataset(cfg)


def key_of(table, df):
    cols = PKS[table]
    return df[cols].astype(str).agg("|".join, axis=1)


# ---- the five labelled cases ------------------------------------------------------------------
def test_five_issue_types_with_the_planned_number_of_cases(ds):
    cfg, d = ds
    counts = d.ground_truth.issue_type.value_counts()
    assert set(counts.index) == set(P.ISSUE_TYPES)
    assert (counts == cases_per_type(cfg)).all()
    assert cases_per_type(build_config("full")) == 10 and cases_per_type(build_config("starter")) == 3


def test_ground_truth_has_the_requested_columns(ds):
    _, d = ds
    gt = d.ground_truth
    assert list(gt.columns) == GROUND_TRUTH_COLUMNS
    assert {"record_id", "table", "issue_type", "description", "expected_decision"} <= set(gt.columns)
    assert not gt.isna().any().any()
    assert gt.record_id.is_unique
    for r in gt.itertuples():
        table, decision, role = P.EXPECTED[r.issue_type]
        assert (r.table, r.expected_decision, r.expected_reviewer_role) == (table, decision, role)
        json.loads(r.injected_changes)


def test_record_ids_point_at_real_rows_in_the_stated_table(ds):
    _, d = ds
    t = d.tables
    for r in d.ground_truth.itertuples():
        if r.table == "daily_cash_position":
            assert r.record_id in set(key_of("daily_cash_position", t["daily_cash_position"]))
        elif r.table == "ledger_entry":
            assert r.record_id in set(t["ledger_entry"].ledger_entry_id)
        else:
            assert r.record_id in set(t["cash_flow_forecast"].cash_flow_forecast_id)


def test_targets_of_different_cases_do_not_overlap(ds):
    _, d = ds
    gt = d.ground_truth
    assert gt.groupby(["table", "record_id"]).size().max() == 1
    ledger_targets = gt[gt.table == "ledger_entry"].record_id
    assert ledger_targets.is_unique
    # case 4 never uses a duplicate row or a duplicated original as its target
    c3 = gt[gt.issue_type == "DOUBLE_COUNTED_CASH_FLOW"]
    originals = {json.loads(x)["repeats"] for x in c3.injected_changes}
    c4 = set(gt[gt.issue_type == "FORECAST_USES_ACTUAL_AFTER_CUTOFF"].record_id)
    assert not (c4 & (originals | set(c3.record_id)))


# ---- only targeted rows change ----------------------------------------------------------------------
def test_only_targeted_rows_differ_from_the_baseline(ds):
    _, d = ds
    gt = d.ground_truth
    for table in d.baseline:
        a, b = d.baseline[table], d.tables[table]
        if table not in PKS:
            pd.testing.assert_frame_equal(a, b)
            continue
        ka, kb = key_of(table, a), key_of(table, b)
        targets = set(gt.record_id[gt.table == table])
        if table == "cash_flow_forecast":
            added = set(kb) - set(ka)
            assert added == set(gt.record_id[gt.issue_type == "DOUBLE_COUNTED_CASH_FLOW"])
            assert set(ka) <= set(kb) and len(b) == len(a) + len(added)
            b = b[kb.isin(ka)]
            kb = kb[kb.isin(ka)]
            targets -= added
        assert list(ka) == list(kb)
        def text(df):
            return df.astype(object).where(df.notna(), "<NA>").astype(str)
        same = text(a).eq(text(b)).all(axis=1)
        changed = set(ka[~same])
        assert changed == targets, table


def test_only_the_documented_columns_change_in_each_target(ds):
    _, d = ds
    for r in d.ground_truth.itertuples():
        changes = json.loads(r.injected_changes)
        if r.issue_type == "DOUBLE_COUNTED_CASH_FLOW":
            continue
        table = r.table
        a, b = d.baseline[table], d.tables[table]
        ka, kb = key_of(table, a), key_of(table, b)
        ra, rb = a[ka == r.record_id].iloc[0], b[kb == r.record_id].iloc[0]
        diff = {c for c in a.columns if str(ra[c]) != str(rb[c]) and not (pd.isna(ra[c]) and pd.isna(rb[c]))}
        assert diff == set(changes), (r.record_id, diff)
        for col, ft in changes.items():
            assert (None if pd.isna(rb[col]) else rb[col]) == ft["to"] or str(rb[col]) == str(ft["to"])


def test_the_duplicate_is_identical_to_its_original_except_for_the_id(ds):
    _, d = ds
    cf = d.tables["cash_flow_forecast"].set_index("cash_flow_forecast_id")
    c3 = d.ground_truth[d.ground_truth.issue_type == "DOUBLE_COUNTED_CASH_FLOW"]
    for r in c3.itertuples():
        orig = json.loads(r.injected_changes)["repeats"]
        pd.testing.assert_series_equal(cf.loc[r.record_id], cf.loc[orig], check_names=False)


def test_the_injector_does_not_modify_the_baseline_it_is_given():
    cfg = build_config("starter")
    base = generate_all(cfg)
    before = {k: v.copy() for k, v in base.items()}
    inject_anomalies(cfg, base, RngStreams(cfg.seed))
    for k in base:
        pd.testing.assert_frame_equal(base[k], before[k])


# ---- ground truth never appears in the inputs ---------------------------------------------------------
LABEL_WORDS = set(P.ISSUE_TYPES) | {d for _, d, _ in P.EXPECTED.values()}


def test_ground_truth_columns_never_appear_in_input_tables(ds):
    _, d = ds
    gt_cols = set(GROUND_TRUTH_COLUMNS) - set()
    for name, df in d.tables.items():
        assert not (set(df.columns) & gt_cols), (name, set(df.columns) & gt_cols)
        assert not any(c.lower() in ("issue_type", "expected_decision", "is_anomaly", "anomaly", "label") for c in df.columns)


def test_no_label_text_leaks_into_any_input_value(ds):
    _, d = ds
    for name, df in d.tables.items():
        for col in df.select_dtypes(include=["object", "string"]).columns:
            vals = set(df[col].dropna().unique())
            assert not (vals & LABEL_WORDS), (name, col)
            joined = " ".join(v for v in vals if isinstance(v, str) and len(v) < 200)
            assert "injected" not in joined.lower() and "anomal" not in joined.lower(), (name, col)


def test_the_database_schema_has_no_ground_truth_columns():
    sql = "\n".join(l for l in (ROOT / "sql" / "schema.sql").read_text().lower().splitlines()
                    if not l.strip().startswith("--"))
    for word in ("issue_type", "expected_decision", "ground_truth", "injected"):
        assert word not in sql


def test_cli_writes_ground_truth_outside_the_input_directory(tmp_path):
    assert main(["--scale", "starter", "--out", str(tmp_path)]) == 0
    assert not list((tmp_path / "input").glob("*ground*"))
    for f in (tmp_path / "input").glob("*.csv"):
        header = f.read_text().splitlines()[0]
        assert "issue_type" not in header and "expected_decision" not in header
    gt = pd.read_csv(tmp_path / "ground_truth" / "ground_truth.csv")
    j = json.loads((tmp_path / "ground_truth" / "ground_truth.json").read_text())
    assert len(gt) == len(j) == 15
    assert list(gt.columns) == GROUND_TRUTH_COLUMNS
    assert isinstance(j[0]["injected_changes"], dict)
    leaked = [p for p in (tmp_path / "input").rglob("*") if p.is_file()
              and any(w in p.read_text() for w in P.ISSUE_TYPES)]
    assert leaked == []


def test_no_anomalies_flag_writes_the_clean_baseline_and_an_empty_ground_truth(tmp_path):
    assert main(["--scale", "starter", "--no-anomalies", "--out", str(tmp_path)]) == 0
    gt = pd.read_csv(tmp_path / "ground_truth" / "ground_truth.csv")
    assert len(gt) == 0 and list(gt.columns) == GROUND_TRUTH_COLUMNS
    base = generate_all(build_config("starter"), reserve_for_anomalies=False)
    assert len(pd.read_csv(tmp_path / "input" / "cash_flow_forecast.csv")) == len(base["cash_flow_forecast"])


# ---- the injected cases are real, and only where labelled ----------------------------------------------------
def test_sql_views_flag_exactly_the_labelled_rows_and_the_baseline_is_clean(ds):
    cfg, d = ds
    gt = d.ground_truth
    clean = create_database()
    load_tables(clean, d.baseline)
    for v in ("v_daily_position_arithmetic_breaks", "v_position_vs_ledger_breaks", "v_forecast_cutoff_violations",
              "v_currency_integrity_issues"):
        assert clean.execute(f"SELECT COUNT(*) FROM {v}").fetchone()[0] == 0, v

    db = create_database()
    load_tables(db, d.tables)
    arith = {f"{a}|{p}" for a, p in db.execute("SELECT bank_account_id, position_date FROM v_daily_position_arithmetic_breaks")}
    assert arith == set(gt.record_id[gt.issue_type == "OPENING_PLUS_LEDGER_NE_CLOSING"])
    cut = {r[0] for r in db.execute("SELECT cash_flow_forecast_id FROM v_forecast_cutoff_violations")}
    assert cut == set(gt.record_id[gt.issue_type == "FORECAST_USES_ACTUAL_AFTER_CUTOFF"])
    fx = {r[0] for r in db.execute("SELECT record_id FROM v_currency_integrity_issues WHERE issue = 'FX_RATE_MISSING'")}
    assert fx == set(gt.record_id[gt.issue_type == "FX_RATE_MISSING"])
    mix = {r[0] for r in db.execute("SELECT record_id FROM v_currency_integrity_issues WHERE issue = 'CURRENCY_MISMATCH'")}
    assert mix == set(gt.record_id[gt.issue_type == "CURRENCY_MIXED_WITHOUT_CONVERSION"])


# ---- randomness ----------------------------------------------------------------------------------------------
def test_same_seed_gives_identical_anomalies(starter_ds):
    cfg, d = starter_ds
    again = generate_dataset(cfg)
    pd.testing.assert_frame_equal(d.ground_truth, again.ground_truth)
    for k in d.tables:
        pd.testing.assert_frame_equal(d.tables[k], again.tables[k])


def test_a_different_seed_chooses_different_targets():
    a = generate_dataset(build_config("starter")).ground_truth
    b = generate_dataset(build_config("starter", seed=7)).ground_truth
    assert set(a.record_id) != set(b.record_id)


def test_the_injector_uses_its_own_stream_only(starter_ds):
    """Draws on other streams, or on the txn stream before it runs, cannot move the targets."""
    cfg, d = starter_ds
    base = generate_all(cfg)
    s = RngStreams(cfg.seed)
    s.txn.random(5000)
    s.forecast.random(5000)
    s.master.random(5000)
    _, truth = inject_anomalies(cfg, base, s)
    pd.testing.assert_frame_equal(truth, d.ground_truth)


def test_the_baseline_is_not_changed_by_turning_injection_on(starter_ds):
    cfg, d = starter_ds
    for k, v in generate_all(cfg).items():
        pd.testing.assert_frame_equal(v, d.baseline[k])


def test_every_anomaly_parameter_is_documented():
    doc = (ROOT / "docs" / "distributions.md").read_text()
    missing = [n for n in vars(P) if n.isupper() and n not in doc]
    assert not missing, missing


def test_published_forecast_table_has_exactly_the_configured_number_of_items(ds):
    cfg, d = ds
    assert len(d.tables["cash_flow_forecast"]) == cfg.n_forecast_items
    assert len(d.baseline["cash_flow_forecast"]) == cfg.n_forecast_items - cases_per_type(cfg)
    assert len(generate_dataset(cfg, inject=False).tables["cash_flow_forecast"]) == cfg.n_forecast_items
