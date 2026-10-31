"""Actual versus forecast: matching, variance arithmetic, derived drivers, metrics."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.compare import generate_comparisons
from src.compare.metrics import _scores, accuracy_reports, daily_variance, item_population
from src.config import build_config
from src.db import create_database, load_tables
from src.generate import main
from src.pipeline import generate_all
from src.txn.common import Calendar


@pytest.fixture(scope="module")
def starter():
    cfg = build_config("starter")
    return cfg, generate_all(cfg)


@pytest.fixture(scope="module")
def full():
    cfg = build_config("full")
    return cfg, generate_all(cfg)


@pytest.fixture(scope="module", params=["starter", "full"])
def built(request, starter, full):
    return starter if request.param == "starter" else full


@pytest.fixture(scope="module")
def reports(full):
    cfg, t = full
    return accuracy_reports(cfg, t)


# ---- structure ------------------------------------------------------------------------
def test_loads_into_sqlite_with_all_constraints(built):
    conn = create_database()
    load_tables(conn, built[1])
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_ids_and_keys(built):
    _, t = built
    v = t["forecast_vs_actual"]
    assert v.comparison_id.str.fullmatch(r"VAR-\d{4}-\d{6}").all() and v.comparison_id.is_unique
    f = v[v.cash_flow_forecast_id.notna()]
    assert not f.duplicated(["cash_flow_forecast_id", "compared_through_date"]).any()


def test_every_forecast_item_has_exactly_one_comparison_row(built):
    _, t = built
    v = t["forecast_vs_actual"]
    f = v[v.cash_flow_forecast_id.notna()]
    assert sorted(f.cash_flow_forecast_id) == sorted(t["cash_flow_forecast"].cash_flow_forecast_id)


def test_compared_through_date_is_after_the_cutoff_and_at_the_horizon(built):
    _, t = built
    v = t["forecast_vs_actual"].merge(t["forecast_run"], on="forecast_run_id")
    assert (v.compared_through_date > v.cutoff_date).all()
    assert (v.compared_through_date == v.horizon_end_date).all()


def test_actuals_are_only_taken_from_after_the_cutoff_up_to_the_horizon(built):
    _, t = built
    v = t["forecast_vs_actual"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date", "horizon_end_date"]],
                                      on="forecast_run_id")
    a = v[v.actual_date.notna()]
    assert (a.actual_date > a.cutoff_date).all() and (a.actual_date <= a.horizon_end_date).all()
    led = t["ledger_entry"].set_index("ledger_entry_id")
    e = a[a.actual_ledger_entry_id.notna()]
    assert (e.actual_ledger_entry_id.map(led.entry_date) == e.actual_date).all()


# ---- arithmetic ---------------------------------------------------------------------------
def test_variance_arithmetic(built):
    _, t = built
    v = t["forecast_vs_actual"]
    assert (v.variance_base_minor == v.actual_net_base_minor - v.forecast_net_base_minor).all()
    assert (v.variance_base_minor == v.amount_variance_base_minor + v.fx_variance_base_minor).all()
    usd = v[v.currency == "USD"]
    assert (usd.fx_variance_base_minor == 0).all()
    sign = np.where(v.direction == "CREDIT", 1, -1)
    assert (np.sign(v.forecast_net_base_minor) * sign >= 0).all()
    assert (np.sign(v.actual_net_base_minor) * sign >= 0).all()


def test_forecast_side_matches_the_item_and_actual_side_matches_the_ledger(built):
    _, t = built
    v = t["forecast_vs_actual"].merge(t["cash_flow_forecast"], on="cash_flow_forecast_id", suffixes=("", "_cf"))
    assert (v.forecast_amount_minor == v.forecast_amount_minor_cf).all()
    assert (v.forecast_date == v.forecast_date_cf).all()
    sign = np.where(v.direction == "CREDIT", 1, -1)
    assert (v.forecast_net_base_minor == sign * v.forecast_base_amount_minor).all()
    assert (v.currency == v.currency_cf).all() and (v.direction == v.direction_cf).all()


def test_every_actual_flow_in_the_window_is_counted_exactly_once(built):
    """Conservation: items + unforecast rows add up to the ledger's flows in the window."""
    cfg, t = built
    led = t["ledger_entry"].copy()
    led["signed"] = np.where(led.direction == "CREDIT", led.base_amount_minor, -led.base_amount_minor)
    links = t["settlement_link"].merge(t["ledger_entry"][["ledger_entry_id", "entry_date"]], on="ledger_entry_id")
    for r in t["forecast_run"].itertuples(index=False):
        w = led[(led.entry_date > r.cutoff_date) & (led.entry_date <= r.horizon_end_date)]
        rows = t["forecast_vs_actual"][t["forecast_vs_actual"].forecast_run_id == r.forecast_run_id]
        n_links = ((links.entry_date > r.cutoff_date) & (links.entry_date <= r.horizon_end_date)).sum()
        assert abs(rows.actual_net_base_minor.sum() - w.signed.sum()) <= n_links, r.forecast_run_id
        un = rows[rows.cash_flow_forecast_id.isna()]
        assert not un.duplicated(["actual_ledger_entry_id", "actual_amount_minor", "variance_driver"]).any()


# ---- drivers: derived from source records ------------------------------------------------------
def test_driver_rules_hold_row_by_row(built):
    _, t = built
    v = t["forecast_vs_actual"].merge(
        t["cash_flow_forecast"][["cash_flow_forecast_id", "source_type", "unadjusted_amount_minor",
                                 "receivable_id", "payable_id"]], on="cash_flow_forecast_id", how="left")
    one_off = set(t["payable"].payable_id[t["payable"].is_one_off == 1])
    d = v.variance_driver
    unf = v.cash_flow_forecast_id.isna()
    assert set(d[unf]) <= {"UNFORECAST_ONE_OFF", "UNFORECAST_OTHER"}
    assert not d[~unf].isin(["UNFORECAST_ONE_OFF", "UNFORECAST_OTHER"]).any()
    assert ((d == "NOT_YET_SETTLED") == (~unf & (v.actual_amount_minor == 0))).all()
    seen = ~unf & (v.actual_amount_minor > 0)
    doc = seen & v.source_type.isin(["RECEIVABLE", "PAYABLE"])
    assert ((d == "PARTIAL_OR_SHORT_PAYMENT") == (doc & (v.actual_amount_minor < v.unadjusted_amount_minor))).all()
    full_pay = doc & (v.actual_amount_minor >= v.unadjusted_amount_minor)
    assert (d[full_pay & (v.date_variance_days > 0)] == "TIMING_LATE").all()
    assert (d[full_pay & (v.date_variance_days < 0)] == "TIMING_EARLY").all()
    assert set(d[v.source_type == "RUN_RATE"]) <= {"NOT_YET_SETTLED", "RUN_RATE_DEVIATION", "FX_MOVEMENT", "ON_TARGET"}
    fx = d == "FX_MOVEMENT"
    assert (v.fx_variance_base_minor[fx] != 0).all() and (v.currency[fx] != "USD").all()
    assert (v.actual_amount_minor[fx] == v.forecast_amount_minor[fx]).all()
    on = d == "ON_TARGET"
    assert (v.variance_base_minor[on] == 0).all()
    uo = d == "UNFORECAST_ONE_OFF"
    pay_of_entry = (t["settlement_link"].dropna(subset=["payable_id"])
                    .groupby("ledger_entry_id").payable_id.apply(lambda s: any(x in one_off for x in s)))
    assert pay_of_entry.reindex(v.actual_ledger_entry_id[uo]).all()


def test_one_off_payment_not_in_a_run_is_labelled_as_such(full):
    cfg, t = full
    cf = t["cash_flow_forecast"]
    one = t["payable"][t["payable"].is_one_off == 1]
    link = t["settlement_link"][t["settlement_link"].payable_id.isin(one.payable_id)]
    led_date = t["ledger_entry"].set_index("ledger_entry_id").entry_date
    runs = t["forecast_run"].set_index("forecast_run_id")
    # a forecast one-off item whose payment actually lands inside the run's window
    cand = cf[cf.payable_id.isin(one.payable_id)].merge(link, on="payable_id")
    cand = cand[(cand.ledger_entry_id.map(led_date) > cand.forecast_run_id.map(runs.cutoff_date))
                & (cand.ledger_entry_id.map(led_date) <= cand.forecast_run_id.map(runs.horizon_end_date))]
    assert len(cand) > 0
    r = cand.iloc[0]
    t2 = {k: x.copy() for k, x in t.items()}
    t2["cash_flow_forecast"] = cf[cf.cash_flow_forecast_id != r.cash_flow_forecast_id]
    again = generate_comparisons(cfg, t2)["forecast_vs_actual"]
    got = again[(again.forecast_run_id == r.forecast_run_id) & (again.actual_ledger_entry_id == r.ledger_entry_id)
                & again.cash_flow_forecast_id.isna()]
    assert len(got) == 1 and got.variance_driver.iloc[0] == "UNFORECAST_ONE_OFF"
    assert got.actual_amount_minor.iloc[0] == r.allocated_amount_minor


def test_changing_the_source_ledger_date_changes_the_driver(starter):
    """Counterfactual: the explanation comes from the records, so editing them moves it."""
    cfg, t = starter
    v = t["forecast_vs_actual"]
    cand = v[(v.cash_flow_forecast_id.notna()) & (v.date_variance_days == 0) & (v.currency == "USD")
             & (v.actual_amount_minor == v.forecast_amount_minor) & v.actual_ledger_entry_id.notna()]
    cand = cand.merge(t["cash_flow_forecast"][["cash_flow_forecast_id", "source_type"]], on="cash_flow_forecast_id")
    cand = cand[cand.source_type != "RUN_RATE"]
    assert len(cand) > 0
    r = cand.iloc[0]
    assert r.variance_driver == "ON_TARGET"
    t2 = {k: x.copy() for k, x in t.items()}
    led = t2["ledger_entry"]
    shifted = (pd.Timestamp(r.actual_date) + pd.Timedelta(days=2)).date().isoformat()
    led.loc[led.ledger_entry_id == r.actual_ledger_entry_id, "entry_date"] = shifted
    again = generate_comparisons(cfg, t2)["forecast_vs_actual"].set_index("cash_flow_forecast_id")
    assert again.loc[r.cash_flow_forecast_id, "variance_driver"] == "TIMING_LATE"
    assert again.loc[r.cash_flow_forecast_id, "date_variance_days"] == 2
    # and earlier instead of later
    led.loc[led.ledger_entry_id == r.actual_ledger_entry_id, "entry_date"] = (
        pd.Timestamp(r.actual_date) - pd.Timedelta(days=1)).date().isoformat()
    again = generate_comparisons(cfg, t2)["forecast_vs_actual"].set_index("cash_flow_forecast_id")
    assert again.loc[r.cash_flow_forecast_id, "variance_driver"] == "TIMING_EARLY"


def test_removing_a_payment_makes_the_item_not_yet_settled_and_halving_it_makes_it_short(starter):
    cfg, t = starter
    v = t["forecast_vs_actual"]
    cand = v[(v.cash_flow_forecast_id.notna()) & (v.variance_driver == "TIMING_LATE")]
    cand = cand.merge(t["cash_flow_forecast"][["cash_flow_forecast_id", "receivable_id", "payable_id"]], on="cash_flow_forecast_id")
    r = cand.iloc[0]
    doc = r.receivable_id if isinstance(r.receivable_id, str) else r.payable_id
    col = "receivable_id" if isinstance(r.receivable_id, str) else "payable_id"
    t2 = {k: x.copy() for k, x in t.items()}
    t2["settlement_link"] = t2["settlement_link"][t2["settlement_link"][col] != doc]
    gone = generate_comparisons(cfg, t2)["forecast_vs_actual"].set_index("cash_flow_forecast_id")
    assert gone.loc[r.cash_flow_forecast_id, "variance_driver"] == "NOT_YET_SETTLED"
    t3 = {k: x.copy() for k, x in t.items()}
    sl = t3["settlement_link"]
    sl.loc[sl[col] == doc, "allocated_amount_minor"] = sl.loc[sl[col] == doc, "allocated_amount_minor"] // 2
    half = generate_comparisons(cfg, t3)["forecast_vs_actual"].set_index("cash_flow_forecast_id")
    assert half.loc[r.cash_flow_forecast_id, "variance_driver"] == "PARTIAL_OR_SHORT_PAYMENT"


def test_drivers_are_reproducible_and_do_not_depend_on_a_random_stream(starter):
    cfg, t = starter
    a = generate_comparisons(cfg, t)["forecast_vs_actual"]
    pd.testing.assert_frame_equal(a, t["forecast_vs_actual"])


def test_timing_and_fx_and_unforecast_drivers_all_occur_at_full_scale(full):
    _, t = full
    d = set(t["forecast_vs_actual"].variance_driver)
    assert {"TIMING_LATE", "TIMING_EARLY", "FX_MOVEMENT", "UNFORECAST_OTHER", "NOT_YET_SETTLED",
            "PARTIAL_OR_SHORT_PAYMENT", "ON_TARGET", "RUN_RATE_DEVIATION", "SCENARIO_AMOUNT_ADJUSTMENT"} <= d


def test_delayed_receipt_scenario_turns_late_timing_into_early_timing(full):
    _, t = full
    v = t["forecast_vs_actual"].merge(t["forecast_run"], on="forecast_run_id") \
        .merge(t["liquidity_scenario"][["scenario_id", "scenario_code"]], on="scenario_id")
    v = v[(v.cutoff_date == "2026-09-30") & (v.version_no == 1)]
    late = v.groupby(["scenario_code", "variance_driver"]).size().unstack(fill_value=0)
    assert late.loc["BASE", "TIMING_LATE"] > late.loc["DELAYED_RECEIPT", "TIMING_LATE"]
    assert late.loc["DELAYED_RECEIPT", "TIMING_EARLY"] > late.loc["BASE", "TIMING_EARLY"]
    assert late.loc["STRESS", "SCENARIO_AMOUNT_ADJUSTMENT"] > 0 and "SCENARIO_AMOUNT_ADJUSTMENT" not in late.columns[late.loc["BASE"] > 0]


# ---- metrics -------------------------------------------------------------------------------------
def test_metric_formulas_on_hand_made_numbers():
    s = _scores(np.array([10, -20]), np.array([100, 80]), np.array([90, 100]))
    assert s["mae_base_minor"] == 15
    assert s["wape"] == pytest.approx(30 / 180)
    assert s["accuracy"] == pytest.approx(1 - 30 / 180)
    assert s["bias_base_minor"] == -5            # forecast too high on balance -> negative
    assert s["mape"] == pytest.approx((10 / 100 + 20 / 80) / 2)
    z = _scores(np.array([5]), np.array([0]), np.array([-5]))
    assert z["mape"] is None and z["wape"] is None


def test_scenario_metrics_match_an_independent_recomputation(full, reports):
    cfg, t = full
    r = t["forecast_run"].merge(t["liquidity_scenario"][["scenario_id", "scenario_code"]], on="scenario_id")
    m = reports["forecast_accuracy_by_scenario"].set_index("scenario_code")
    v = t["forecast_vs_actual"]
    for code in ("BASE", "DELAYED_RECEIPT", "STRESS"):
        runs = r[(r.scenario_code == code) & (r.status == "APPROVED")].forecast_run_id
        f = v[v.forecast_run_id.isin(runs) & v.cash_flow_forecast_id.notna()]
        f = f[(f.forecast_date <= f.compared_through_date) | (f.actual_amount_minor > 0)]
        err = (f.actual_net_base_minor - f.forecast_net_base_minor).abs()
        assert m.loc[code, "item_n"] == len(f)
        assert m.loc[code, "item_mae_base_minor"] == pytest.approx(err.mean())
        assert m.loc[code, "item_wape"] == pytest.approx(err.sum() / f.actual_net_base_minor.abs().sum())
        assert m.loc[code, "item_accuracy"] == pytest.approx(1 - m.loc[code, "item_wape"])
        assert m.loc[code, "item_bias_base_minor"] == pytest.approx((f.actual_net_base_minor - f.forecast_net_base_minor).mean())
        assert m.loc[code, "runs_scored"] == len(runs)


def test_only_approved_runs_are_scored(full, reports):
    _, t = full
    by_run = reports["forecast_accuracy_by_run"]
    assert set(by_run.forecast_run_id) == set(t["forecast_run"].forecast_run_id)
    assert (by_run.scored == (by_run.status == "APPROVED")).all()
    assert not by_run[by_run.status == "SUPERSEDED"].scored.any()


def test_accuracy_ordering_is_sensible_and_values_are_in_range(reports):
    m = reports["forecast_accuracy_by_scenario"].set_index("scenario_code")
    assert (m.item_wape > 0).all() and (m.item_wape < 1.5).all()
    assert (m.item_accuracy < 1).all()
    assert (m.all_flows_wape >= m.item_wape * 0.5).all()
    # stress scenario is deliberately pessimistic, so it is the least accurate against this data
    assert m.loc["STRESS", "item_accuracy"] < m.loc["BASE", "item_accuracy"]


def test_daily_variance_matches_the_ledger_and_the_projection(full, reports):
    cfg, t = full
    d = reports["forecast_variance_daily"]
    assert len(d) == len(t["forecast_daily_projection"])
    assert (d.net_variance_base_minor == d.actual_net_base_minor - d.forecast_net_base_minor).all()
    sample = d.sample(200, random_state=3)
    led = t["ledger_entry"].merge(t["bank_account"][["bank_account_id", "entity_id"]], on="bank_account_id")
    for r in sample.itertuples():
        x = led[(led.entity_id == r.entity_id) & (led.entry_date == r.date)]
        assert r.actual_inflow_base_minor == x.base_amount_minor[x.direction == "CREDIT"].sum()
        assert r.actual_outflow_base_minor == x.base_amount_minor[x.direction == "DEBIT"].sum()
    proj = t["forecast_daily_projection"].set_index(["forecast_run_id", "entity_id", "projection_date"])
    key = list(zip(d.forecast_run_id, d.entity_id, d.date))
    assert (proj.loc[key, "projected_inflow_base_minor"].to_numpy() == d.forecast_inflow_base_minor.to_numpy()).all()


def test_reports_are_written_by_the_cli(tmp_path):
    assert main(["--scale", "starter", "--out", str(tmp_path)]) == 0
    j = json.loads((tmp_path / "derived" / "forecast_accuracy_by_scenario.json").read_text())
    assert [r["scenario_code"] for r in j] == ["BASE", "DELAYED_RECEIPT", "STRESS"]
    assert all("item_wape" in r and "item_bias_base_minor" in r for r in j)
    assert (tmp_path / "derived" / "forecast_variance_daily.csv").exists()
