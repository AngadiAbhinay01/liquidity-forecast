"""Scenarios, versioned forecast runs, cash-flow items and projected liquidity."""
import inspect
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import build_config
from src.db import create_database, load_tables
from src.forecast import generate_forecasts
from src.forecast import params as P
from src.forecast import runs as runs_mod
from src.forecast import scenarios as sc
from src.forecast.candidates import Knowledge
from src.forecast.runs import build_items, run_events
from src.forecast.scenarios import ScenarioParams
from src.pipeline import generate_all
from src.rng import RngStreams
from src.txn.common import Calendar, RateBook

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def starter():
    cfg = build_config("starter")
    return cfg, generate_all(cfg, reserve_for_anomalies=False)


@pytest.fixture(scope="module")
def full():
    cfg = build_config("full")
    return cfg, generate_all(cfg, reserve_for_anomalies=False)


@pytest.fixture(scope="module", params=["starter", "full"])
def built(request, starter, full):
    return starter if request.param == "starter" else full


@pytest.fixture(scope="module")
def db(built):
    conn = create_database()
    load_tables(conn, built[1])
    return conn


def D(s):
    return date.fromisoformat(s)


def next_bd(d):
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def run_items(t, run_id):
    return t["cash_flow_forecast"][t["cash_flow_forecast"].forecast_run_id == run_id]


def runs_by_cutoff(t):
    r = t["forecast_run"].merge(t["liquidity_scenario"][["scenario_id", "scenario_code"]], on="scenario_id")
    return r


# ---- size, keys, formats ----------------------------------------------------------------
def test_exactly_the_configured_number_of_forecast_items(built):
    cfg, t = built
    assert len(t["cash_flow_forecast"]) == cfg.n_forecast_items


@pytest.mark.parametrize("n", [100, 1234, 2500])
def test_item_budget_is_met_exactly_for_other_sizes(n):
    cfg = build_config("starter", n_forecast_items=n)
    t = generate_all(cfg, reserve_for_anomalies=False)
    assert len(t["cash_flow_forecast"]) == n


def test_a_budget_larger_than_the_data_can_supply_is_refused_clearly():
    with pytest.raises(ValueError, match="forecast item budget|can supply"):
        generate_all(build_config("starter", n_forecast_items=50_000), reserve_for_anomalies=False)


def test_ids_have_the_documented_formats(built):
    _, t = built
    runs = t["forecast_run"]
    assert runs.forecast_run_id.str.fullmatch(r"FCAST-\d{4}-\d{2}-\d{2}-\d{3}").all()
    assert (runs.forecast_run_id == "FCAST-" + runs.cutoff_date + "-" + runs.run_seq.astype(str).str.zfill(3)).all()
    cf = t["cash_flow_forecast"]
    assert cf.cash_flow_forecast_id.str.fullmatch(r"CF-\d{4}-\d{6}").all()
    assert cf.cash_flow_forecast_id.is_unique and runs.forecast_run_id.is_unique
    for year, g in cf.groupby(cf.cash_flow_forecast_id.str[3:7]):
        assert g.cash_flow_forecast_id.str[-6:].astype(int).tolist() == list(range(1, len(g) + 1))


def test_run_sequence_numbers_are_gap_free_per_cutoff(built):
    _, t = built
    for _, g in t["forecast_run"].groupby("cutoff_date"):
        assert sorted(g.run_seq) == list(range(1, len(g) + 1))


def test_everything_loads_and_foreign_keys_hold(db):
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


# ---- scenarios ----------------------------------------------------------------------------------
def test_three_scenarios_each_defined_by_the_four_parameters(built):
    _, t = built
    s = t["liquidity_scenario"].set_index("scenario_code")
    assert list(s.index) == ["BASE", "DELAYED_RECEIPT", "STRESS"]
    # documented values (docs/distributions.md), written out on purpose
    assert s.loc["BASE", ["receipt_delay_days", "collection_haircut_bps", "fx_shock_bps", "one_off_multiplier_bps"]].tolist() == [0, 0, 0, 10000]
    assert s.loc["DELAYED_RECEIPT", ["receipt_delay_days", "collection_haircut_bps", "fx_shock_bps", "one_off_multiplier_bps"]].tolist() == [14, 0, 0, 10000]
    assert s.loc["STRESS", ["receipt_delay_days", "collection_haircut_bps", "fx_shock_bps", "one_off_multiplier_bps"]].tolist() == [21, 1000, -1000, 15000]
    assert (s.assumption_notes.str.len() > 40).all()


def test_stored_params_json_matches_the_scenario_row(built):
    import json
    _, t = built
    sc_rows = t["liquidity_scenario"].set_index("scenario_id")
    for r in t["forecast_run"].itertuples(index=False):
        row = sc_rows.loc[r.scenario_id]
        assert json.loads(r.scenario_params_json) == {
            "receipt_delay_days": int(row.receipt_delay_days),
            "collection_haircut_bps": int(row.collection_haircut_bps),
            "fx_shock_bps": int(row.fx_shock_bps),
            "one_off_multiplier_bps": int(row.one_off_multiplier_bps)}


def test_adjust_receives_nothing_about_the_scenario_except_its_four_parameters():
    sig = inspect.signature(sc.adjust)
    assert list(sig.parameters) == ["cands", "params", "cutoff_day", "cal", "rb"]
    assert sig.parameters["params"].annotation in (ScenarioParams, "ScenarioParams")
    assert [f for f in ScenarioParams.__dataclass_fields__] == [
        "receipt_delay_days", "collection_haircut_bps", "fx_shock_bps", "one_off_multiplier_bps"]
    src = inspect.getsource(sc.adjust)
    assert "scenario_code" not in src and "scenario_id" not in src and "BASE" not in src


@pytest.fixture(scope="module")
def ctx(full):
    cfg, t = full
    cal = Calendar(cfg)
    rb = RateBook(cfg, t["currency_rate"], cal)
    know = Knowledge(cal, rb, t, cfg.n_days)
    c = cal.offset("2026-06-30")
    return cfg, t, cal, rb, know, c, know.candidates(c, cfg.forecast_horizon_days)


def test_scenarios_at_one_cutoff_use_the_same_items_in_the_same_order(full):
    _, t = full
    r = runs_by_cutoff(t)
    r = r[(r.cutoff_date == "2026-09-30") & (r.version_no == 1)]
    assert len(r) == 3
    shared = ["bank_account_id", "source_type", "receivable_id", "payable_id", "source_ledger_entry_id",
              "source_data_date", "direction", "currency", "expected_date", "unadjusted_amount_minor", "fx_rate_id"]
    frames = [run_items(t, rid)[shared].reset_index(drop=True) for rid in r.forecast_run_id]
    for f in frames[1:]:
        pd.testing.assert_frame_equal(frames[0], f)


def test_a_scenario_with_the_same_four_numbers_gives_the_same_forecast(ctx):
    cfg, t, cal, rb, know, c, cands = ctx
    stress = ScenarioParams(21, 1000, -1000, 15000)
    a = build_items(cands, stress, c, cal, rb, None)
    b = build_items(cands, ScenarioParams(**stress.__dict__), c, cal, rb, None)
    pd.testing.assert_frame_equal(a, b)
    run = runs_by_cutoff(t)
    run = run[(run.cutoff_date == "2026-06-30") & (run.scenario_code == "STRESS")].forecast_run_id.iloc[0]
    stored = run_items(t, run).reset_index(drop=True)
    for col in stored.columns.difference(["cash_flow_forecast_id", "forecast_run_id"]):
        assert (stored[col].fillna("-") == a[col].fillna("-")).all(), col


def _changed(base_items, other_items):
    cols = [c for c in base_items.columns if not c.startswith("_")]
    return {c for c in cols if not (base_items[c].fillna("-") == other_items[c].fillna("-")).all()}


def test_each_parameter_changes_only_its_documented_columns(ctx):
    cfg, t, cal, rb, know, c, cands = ctx
    base = build_items(cands, ScenarioParams(), c, cal, rb, None)
    assert cands.is_one_off.sum() > 0 and (cands.source_type == "RECEIVABLE").sum() > 0
    d = build_items(cands, ScenarioParams(receipt_delay_days=10), c, cal, rb, None)
    assert _changed(base, d) == {"forecast_date"}
    assert (d.source_type[d.forecast_date != base.forecast_date] == "RECEIVABLE").all()
    h = build_items(cands, ScenarioParams(collection_haircut_bps=2500), c, cal, rb, None)
    assert _changed(base, h) == {"forecast_amount_minor", "forecast_base_amount_minor"}
    assert (h.source_type[h.forecast_amount_minor != base.forecast_amount_minor] == "RECEIVABLE").all()
    f = build_items(cands, ScenarioParams(fx_shock_bps=-700), c, cal, rb, None)
    assert _changed(base, f) == {"forecast_base_amount_minor"}
    assert (f.currency[f.forecast_base_amount_minor != base.forecast_base_amount_minor] != "USD").all()
    assert (f.forecast_base_amount_minor[f.currency == "USD"] == base.forecast_base_amount_minor[f.currency == "USD"]).all()
    m = build_items(cands, ScenarioParams(one_off_multiplier_bps=20000), c, cal, rb, None)
    assert _changed(base, m) == {"forecast_amount_minor", "forecast_base_amount_minor"}
    one_off = cands.is_one_off.to_numpy() == 1
    assert ((m.forecast_amount_minor != base.forecast_amount_minor).to_numpy() <= one_off).all()
    assert (m.forecast_amount_minor.to_numpy()[one_off] == 2 * base.forecast_amount_minor.to_numpy()[one_off]).all()


def test_stored_items_follow_the_documented_rules_for_each_scenario(full):
    """Independent re-computation of dates and amounts from the stored parameters."""
    cfg, t = full
    rates = t["currency_rate"].set_index("rate_id").rate
    exp = {c.code: c.minor_unit_exponent for c in cfg.currencies}
    one_off_ids = set(t["payable"].payable_id[t["payable"].is_one_off == 1])
    for run in runs_by_cutoff(t).itertuples(index=False):
        p = eval(run.scenario_params_json.replace("null", "None"))
        it = run_items(t, run.forecast_run_id)
        rcv = it.source_type == "RECEIVABLE"
        c = D(run.cutoff_date)
        base_day = pd.Series([max(D(e), next_bd(c + timedelta(days=1))) for e in it.expected_date], index=it.index)
        want = [next_bd(b + timedelta(days=p["receipt_delay_days"] if r else 0)) for b, r in zip(base_day, rcv)]
        assert [D(x) for x in it.forecast_date] == want
        amt = it.unadjusted_amount_minor.astype(float)
        amt = np.where(rcv, amt * (10000 - p["collection_haircut_bps"]) / 10000, amt)
        isone = it.payable_id.isin(one_off_ids)
        amt = np.where(isone, amt * p["one_off_multiplier_bps"] / 10000, amt)
        assert (it.forecast_amount_minor == np.rint(amt)).all()
        nb = it[it.currency != "USD"]
        rate = nb.fx_rate_id.map(rates) * (10000 + p["fx_shock_bps"]) / 10000
        want_base = np.rint(nb.forecast_amount_minor * rate * 10.0 ** (2 - nb.currency.map(exp)))
        assert (nb.forecast_base_amount_minor == want_base).all()
        us = it[it.currency == "USD"]
        assert (us.forecast_base_amount_minor == us.forecast_amount_minor).all()


def test_base_scenario_is_neutral(full):
    _, t = full
    base = runs_by_cutoff(t)
    base = base[base.scenario_code == "BASE"].forecast_run_id
    it = t["cash_flow_forecast"][t["cash_flow_forecast"].forecast_run_id.isin(base)]
    assert (it.forecast_amount_minor == it.unadjusted_amount_minor).all()


def test_stress_is_worse_than_delayed_is_worse_than_base(full):
    _, t = full
    r = runs_by_cutoff(t)
    r = r[(r.cutoff_date == "2026-09-30") & (r.version_no == 1)].set_index("scenario_code").forecast_run_id
    p = t["forecast_daily_projection"]
    short = {k: p[p.forecast_run_id == v].shortfall_flag.mean() for k, v in r.items()}
    assert short["BASE"] < short["DELAYED_RECEIPT"] < short["STRESS"]
    inflow = {k: p[p.forecast_run_id == v].projected_inflow_base_minor.sum() for k, v in r.items()}
    assert inflow["BASE"] > inflow["DELAYED_RECEIPT"] > inflow["STRESS"]


# ---- cutoff discipline -------------------------------------------------------------------------------
def test_no_item_uses_data_after_its_cutoff(built, db):
    _, t = built
    cf = t["cash_flow_forecast"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id")
    assert (cf.source_data_date <= cf.cutoff_date).all()
    assert (cf.forecast_date > cf.cutoff_date).all()
    led = t["ledger_entry"].set_index("ledger_entry_id").entry_date
    has = cf.source_ledger_entry_id.notna()
    assert (cf.source_ledger_entry_id[has].map(led) <= cf.cutoff_date[has]).all()
    assert (cf.source_ledger_entry_id[has].map(led) <= cf.source_data_date[has]).all()
    fx = cf.fx_rate_id.dropna()
    assert (fx.str[8:].str.replace(r"(\d{4})(\d{2})(\d{2})", r"\1-\2-\3", regex=True) <= cf.cutoff_date[fx.index]).all()
    assert db.execute("SELECT COUNT(*) FROM v_forecast_cutoff_violations").fetchone()[0] == 0


def test_changing_everything_after_the_cutoff_changes_nothing(ctx):
    """Corrupt every record dated after the cutoff; the candidates must not move."""
    cfg, t, cal, rb, know, c, cands = ctx
    cut = cal.iso(c)
    t2 = {k: v.copy() for k, v in t.items()}
    led = t2["ledger_entry"]
    after = led.entry_date > cut
    led.loc[after, "amount_minor"] = led.loc[after, "amount_minor"] * 7 + 1
    led.loc[after, "direction"] = np.where(led.loc[after, "direction"] == "CREDIT", "DEBIT", "CREDIT")
    for ev in ("receivable_status_event", "payable_status_event"):
        e = t2[ev]
        e.loc[e.event_date > cut, "to_status"] = "SETTLED"
    pos = t2["daily_cash_position"]
    pos.loc[pos.position_date > cut, "closing_minor"] = 0
    # ordinary documents invoiced after the cutoff do not exist yet; scheduled commitments do
    for tab, col, idc in (("receivable", "invoice_date", "receivable_id"), ("payable", "invoice_date", "payable_id")):
        d = t2[tab]
        drop = (d[col] > cut)
        if tab == "payable":
            drop &= ~(d.payable_category.isin(["PAYROLL", "DEBT_SERVICE", "TAX"]) | (d.is_one_off == 1))
        t2[tab] = d[~drop]
    links = t2["settlement_link"]
    links.loc[links.ledger_entry_id.isin(led.ledger_entry_id[after]), "allocated_amount_minor"] += 12345
    know2 = Knowledge(cal, rb, t2, cfg.n_days)
    again = know2.candidates(c, cfg.forecast_horizon_days)
    pd.testing.assert_frame_equal(cands, again)
    a = know.opening_positions(c)
    b = know2.opening_positions(c)
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))


def test_candidates_do_move_when_data_before_the_cutoff_changes(ctx):
    cfg, t, cal, rb, know, c, cands = ctx
    t2 = {k: v.copy() for k, v in t.items()}
    links = t2["settlement_link"]
    links["allocated_amount_minor"] = links.allocated_amount_minor // 2 + 1
    again = Knowledge(cal, rb, t2, cfg.n_days).candidates(c, cfg.forecast_horizon_days)
    assert not again.equals(cands)


def test_items_are_limited_to_open_documents_that_exist_by_the_cutoff(full):
    _, t = full
    cf = t["cash_flow_forecast"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id")
    r = t["receivable"].set_index("receivable_id")
    rc = cf[cf.source_type == "RECEIVABLE"]
    assert (rc.receivable_id.map(r.invoice_date) <= rc.cutoff_date).all()
    paid = (t["settlement_link"].dropna(subset=["receivable_id"])
            .merge(t["ledger_entry"][["ledger_entry_id", "entry_date"]], on="ledger_entry_id"))
    first_full = {}
    for rid, g in paid.groupby("receivable_id"):
        first_full[rid] = g.sort_values("entry_date")
    # a receivable fully settled on or before the cutoff must not be forecast
    amount = r.amount_minor
    for row in rc.sample(300, random_state=1).itertuples():
        g = first_full.get(row.receivable_id)
        settled = 0 if g is None else g[g.entry_date <= row.cutoff_date].allocated_amount_minor.sum()
        assert settled < amount[row.receivable_id]
        assert row.unadjusted_amount_minor == amount[row.receivable_id] - settled


def test_documents_below_materiality_are_not_forecast_individually(full):
    cfg, t = full
    rates = t["currency_rate"].set_index("rate_id").rate
    cf = t["cash_flow_forecast"]
    docs = cf[cf.source_type != "RUN_RATE"]
    exp = {c.code: c.minor_unit_exponent for c in cfg.currencies}
    usd = docs.unadjusted_amount_minor * docs.fx_rate_id.map(rates).fillna(1.0) / 10.0 ** docs.currency.map(exp)
    assert usd.min() >= P.FORECAST_MATERIALITY_USD * 0.999


def test_scheduled_payables_and_one_offs_are_forecast_before_their_payment_date(full):
    _, t = full
    cf = t["cash_flow_forecast"].merge(t["payable"], on="payable_id")
    sched = cf[cf.payable_category.isin(["PAYROLL", "DEBT_SERVICE", "TAX"]) | (cf.is_one_off == 1)]
    assert len(sched) > 100 and (sched.is_one_off == 1).any()
    assert (sched.source_data_date <= (pd.to_datetime(sched.due_date) - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")).all()


def test_run_rate_items_come_from_trailing_regular_flows(built):
    _, t = built
    cf = t["cash_flow_forecast"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id")
    rr = cf[cf.source_type == "RUN_RATE"]
    assert len(rr) > 0 and rr.receivable_id.isna().all() and rr.payable_id.isna().all()
    led = t["ledger_entry"].set_index("ledger_entry_id")
    src = led.loc[rr.source_ledger_entry_id]
    assert src.entry_type.isin(["OPERATING_INFLOW", "OPERATING_OUTFLOW"]).all()
    assert (src.bank_account_id.to_numpy() == rr.bank_account_id.to_numpy()).all()
    assert (src.direction.to_numpy() == rr.direction.to_numpy()).all()
    age = (pd.to_datetime(rr.cutoff_date).to_numpy() - pd.to_datetime(src.entry_date).to_numpy()).astype("timedelta64[D]").astype(int)
    assert ((age >= 0) & (age < P.RUN_RATE_LOOKBACK_DAYS)).all()
    assert (rr.expected_date <= rr.forecast_date).all()


def test_item_currency_direction_and_account_rules(built):
    _, t = built
    cf = t["cash_flow_forecast"]
    acc = t["bank_account"].set_index("bank_account_id")
    assert (cf.currency == cf.bank_account_id.map(acc.currency)).all()
    assert (cf.direction[cf.source_type == "RECEIVABLE"] == "CREDIT").all()
    assert (cf.direction[cf.source_type == "PAYABLE"] == "DEBIT").all()
    assert (cf.forecast_date >= cf.expected_date).all()
    assert (cf.forecast_amount_minor >= 0).all() and (cf.unadjusted_amount_minor > 0).all()
    ended = acc.effective_to.dropna()
    # Selection cannot depend on the scenario, so a *delayed* receipt may land after its account
    # closed (documented). Everything else must fall inside the account's life.
    code = cf.forecast_run_id.map(runs_by_cutoff(t).set_index("forecast_run_id").scenario_code)
    e = cf[cf.bank_account_id.isin(ended.index) & ((code == "BASE") | (cf.source_type != "RECEIVABLE"))]
    assert len(e) > 0
    assert (e.forecast_date <= e.bank_account_id.map(ended)).all()
    a = cf.merge(t["receivable"][["receivable_id", "bank_account_id"]], on="receivable_id", how="inner", suffixes=("", "_doc"))
    assert (a.bank_account_id == a.bank_account_id_doc).all()
    assert not cf.duplicated(["forecast_run_id", "receivable_id", "payable_id", "source_type", "bank_account_id", "direction", "expected_date", "forecast_date"]).any()


# ---- versions and history ---------------------------------------------------------------------------------
def test_versioned_runs_keep_both_versions_and_chain_them(full):
    _, t = full
    r = t["forecast_run"].set_index("forecast_run_id")
    v2 = r[r.version_no == 2]
    assert len(v2) >= 1
    for rid, row in v2.iterrows():
        old = r.loc[row.supersedes_run_id]
        assert old.version_no == 1 and old.scenario_id == row.scenario_id and old.cutoff_date == row.cutoff_date
        assert old.status == "SUPERSEDED" and row.status in ("APPROVED", "COMPLETED")
        assert len(run_items(t, row.supersedes_run_id)) > 0 and len(run_items(t, rid)) > 0   # nothing overwritten
        assert old.run_checksum == row.run_checksum                                           # same inputs, same hash
    assert (r.supersedes_run_id.isna() == (r.version_no == 1)).all()


def test_checksums_differ_when_the_scenario_differs(full):
    _, t = full
    r = runs_by_cutoff(t)
    g = r[(r.cutoff_date == "2026-09-30")]
    assert g.run_checksum.nunique() == 3


def test_run_history_is_gap_free_legal_and_ends_at_the_stored_status(built):
    _, t = built
    ev = t["forecast_run_status_event"].sort_values(["forecast_run_id", "event_seq"])
    assert set(ev.forecast_run_id) == set(t["forecast_run"].forecast_run_id)
    for rid, g in ev.groupby("forecast_run_id"):
        assert g.event_seq.tolist() == list(range(1, len(g) + 1))
        assert g.from_status.iloc[0] is None or pd.isna(g.from_status.iloc[0])
        run_events([(s, 0) for s in g.to_status])                 # raises on any illegal transition
        assert g.from_status.iloc[1:].tolist() == g.to_status.iloc[:-1].tolist()
        assert g.event_date.is_monotonic_increasing
        approvals = g[g.cause.isin(["REVIEW_APPROVED", "REVIEW_REJECTED"])]
        assert (approvals.actor_role == "RISK_COMPLIANCE_REVIEWER").all()
    last = ev.groupby("forecast_run_id").to_status.last()
    runs = t["forecast_run"].set_index("forecast_run_id")
    assert (runs.status == last.reindex(runs.index)).all()


def test_forecast_items_cannot_be_changed_after_creation(db):
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE cash_flow_forecast SET forecast_amount_minor = 1")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM cash_flow_forecast")


def test_cutoffs_are_last_business_days_with_a_full_horizon(built):
    cfg, t = built
    cut = pd.to_datetime(t["forecast_run"].cutoff_date.unique())
    assert (cut.weekday < 5).all()
    # last business day of its month: every later day of that month is a weekend day
    month_end = cut + pd.offsets.MonthEnd(0)
    for c, e in zip(cut, month_end):
        assert all(d.weekday() >= 5 for d in pd.date_range(c + pd.Timedelta(days=1), e))
    assert (cut <= pd.Timestamp(cfg.last_cutoff_date)).all()
    r = t["forecast_run"]
    assert (pd.to_datetime(r.horizon_end_date) == pd.to_datetime(r.cutoff_date) + pd.Timedelta(days=r.horizon_days.iloc[0])).all()


def test_the_budget_limited_run_holds_exactly_the_largest_exposures(full):
    cfg, t = full
    cal = Calendar(cfg)
    rb = RateBook(cfg, t["currency_rate"], cal)
    know = Knowledge(cal, rb, t, cfg.n_days)
    cnt = t["cash_flow_forecast"].groupby("forecast_run_id").size()
    runs = t["forecast_run"].set_index("forecast_run_id")
    per_cut = runs.groupby("cutoff_date").size()
    only = per_cut[per_cut == 1].index
    assert len(only) == 1                        # the one cutoff that took the leftover budget
    rid = runs.index[runs.cutoff_date == only[0]][0]
    c = cal.offset(only[0])
    cands = know.candidates(c, cfg.forecast_horizon_days)
    m = int(cnt[rid])
    assert m < len(cands)
    top = cands.sort_values(["usd_size", "key"], ascending=[False, True]).head(m)
    got = run_items(t, rid)
    want = set(zip(top.account_id, top.source_type, top.receivable_id.fillna("-"), top.payable_id.fillna("-"),
                   top.direction, [cal.iso(d) for d in top.expected_day]))
    have = set(zip(got.bank_account_id, got.source_type, got.receivable_id.fillna("-"), got.payable_id.fillna("-"),
                   got.direction, got.expected_date))
    assert have == want
    assert runs.loc[rid, "status"] == "COMPLETED"
    assert cnt.sum() == cfg.n_forecast_items


# ---- projections -------------------------------------------------------------------------------------------------
def test_projection_covers_every_day_of_the_horizon_for_each_live_entity(built):
    cfg, t = built
    p = t["forecast_daily_projection"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id")
    d = (pd.to_datetime(p.projection_date) - pd.to_datetime(p.cutoff_date)).dt.days
    assert d.min() == 1 and d.max() == cfg.forecast_horizon_days
    n = p.groupby(["forecast_run_id", "entity_id"]).size()
    assert (n == cfg.forecast_horizon_days).all()
    assert not p.duplicated(["forecast_run_id", "entity_id", "projection_date"]).any()


def test_projection_arithmetic_chain_and_first_day_equals_the_cutoff_position(built):
    cfg, t = built
    p = t["forecast_daily_projection"].sort_values(["forecast_run_id", "entity_id", "projection_date"])
    assert (p.projected_closing_base_minor == p.opening_base_minor + p.projected_inflow_base_minor
            - p.projected_outflow_base_minor).all()
    prev = p.groupby(["forecast_run_id", "entity_id"]).projected_closing_base_minor.shift(1)
    cont = p.opening_base_minor[prev.notna()] == prev[prev.notna()]
    assert cont.all()
    assert (p.shortfall_flag == (p.projected_closing_base_minor < p.liquidity_buffer_base_minor).astype(int)).all()
    base_runs = runs_by_cutoff(t)
    base_runs = base_runs[base_runs.scenario_code == "BASE"][["forecast_run_id", "cutoff_date"]]
    pos = t["daily_cash_position"].merge(t["bank_account"][["bank_account_id", "entity_id"]], on="bank_account_id")
    for r in base_runs.itertuples(index=False):
        first = p[(p.forecast_run_id == r.forecast_run_id)].groupby("entity_id").opening_base_minor.first()
        want = pos[pos.position_date == r.cutoff_date].groupby("entity_id").closing_base_minor.sum()
        assert (first == want.reindex(first.index)).all()


def test_fx_shock_also_revalues_the_opening_balance(full):
    cfg, t = full
    r = runs_by_cutoff(t)
    r = r[(r.cutoff_date == "2026-09-30") & (r.scenario_code == "STRESS")].iloc[0]
    rates = t["currency_rate"].set_index(["quote_currency", "rate_date"]).rate
    exp = {c.code: c.minor_unit_exponent for c in cfg.currencies}
    pos = t["daily_cash_position"][t["daily_cash_position"].position_date == r.cutoff_date]
    pos = pos.merge(t["bank_account"][["bank_account_id", "entity_id"]], on="bank_account_id")
    pos["rate"] = [1.0 if c == "USD" else rates[(c, r.cutoff_date)] * 0.9 for c in pos.currency]
    pos["v"] = np.rint(pos.closing_minor * pos.rate * 10.0 ** (2 - pos.currency.map(exp)))
    want = pos.groupby("entity_id").v.sum()
    p = t["forecast_daily_projection"]
    first = p[p.forecast_run_id == r.forecast_run_id].groupby("entity_id").opening_base_minor.first()
    assert (first == want.reindex(first.index)).all()


def test_projection_flows_equal_the_item_totals(built):
    cfg, t = built
    p = t["forecast_daily_projection"]
    cf = t["cash_flow_forecast"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date", "horizon_end_date"]], on="forecast_run_id")
    cf = cf.merge(t["bank_account"][["bank_account_id", "entity_id"]], on="bank_account_id")
    cf = cf[cf.forecast_date <= cf.horizon_end_date]
    for direction, col in (("CREDIT", "projected_inflow_base_minor"), ("DEBIT", "projected_outflow_base_minor")):
        got = cf[cf.direction == direction].groupby(["forecast_run_id", "entity_id", "forecast_date"]).forecast_base_amount_minor.sum()
        have = p.set_index(["forecast_run_id", "entity_id", "projection_date"])[col]
        have = have[have > 0]
        assert have.index.equals(got.index) or (have.sort_index().to_numpy() == got.sort_index().to_numpy()).all()
        assert (have.sort_index() == got.sort_index()).all()


def test_shortfalls_exist_but_are_not_everywhere(full):
    _, t = full
    f = t["forecast_daily_projection"].shortfall_flag.mean()
    assert 0.02 < f < 0.6


# ---- determinism and independence -----------------------------------------------------------------------------------
def test_same_seed_same_forecast_tables(starter):
    cfg, t = starter
    again = generate_all(cfg, reserve_for_anomalies=False)
    for k in ("liquidity_scenario", "forecast_run", "forecast_run_status_event", "cash_flow_forecast",
              "forecast_daily_projection"):
        pd.testing.assert_frame_equal(t[k], again[k])


def test_forecast_stage_does_not_change_earlier_tables(starter):
    cfg, t = starter
    from src.master import generate_master
    from src.txn import generate_transactions
    s = RngStreams(cfg.seed)
    m = generate_master(cfg, s)
    x = generate_transactions(cfg, m, s)
    pd.testing.assert_frame_equal(x["ledger_entry"], t["ledger_entry"])


def test_every_forecast_parameter_is_documented():
    doc = (ROOT / "docs" / "distributions.md").read_text()
    names = [n for n in vars(P) if n.isupper()]
    missing = [n for n in names if n not in doc]
    assert not missing, missing
