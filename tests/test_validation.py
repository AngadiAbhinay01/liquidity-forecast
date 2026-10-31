"""Validation module tests: baseline passes, each rule fails when broken by hand, anomalies caught only where labelled."""
import ast
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import build_config
from src.generate import main as generate_main
from src.pipeline import generate_all, generate_dataset
from src.txn import events as txn_events
from src.forecast import runs as forecast_runs
from src.validate import CATEGORIES, REGISTRY, all_violations, report_markdown, run_checks, summary
from src.validate import checks as C
from src.validate.__main__ import load_all, main as validate_main
from src.validate.labels import EXPECTED_CHECK, compare

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def ds():
    return generate_dataset(build_config("starter"))


@pytest.fixture(scope="module")
def base(ds):
    return ds.baseline


@pytest.fixture(scope="module")
def injected(ds):
    return ds.tables


def mutate(base, **edits):
    """Copy of the baseline in which the named tables are replaced by edited copies."""
    t = dict(base)
    for name, fn in edits.items():
        df = base[name].copy()
        out = fn(df)
        t[name] = df if out is None else out
    return t


def flagged(tables, check_id):
    """Records flagged by one check (running only that check keeps these tests fast)."""
    fn = next(c.fn for c in REGISTRY if c.check_id == check_id)
    _, v = fn(tables)
    return set(zip(v.table_name, v.record_id))


# ---- baseline and anomalies -----------------------------------------------------------------
def test_baseline_passes_every_check(base):
    outcomes = run_checks(base)
    assert all_violations(outcomes).empty
    assert all(o.n_checked > 0 for o in outcomes), [o.check_id for o in outcomes if o.n_checked == 0]


def test_every_category_has_at_least_one_check():
    assert {c.category for c in REGISTRY} == set(CATEGORIES)
    assert len({c.check_id for c in REGISTRY}) == len(REGISTRY)


def test_injected_data_is_flagged_only_at_labelled_records(ds, injected):
    outcomes = run_checks(injected)
    v = all_violations(outcomes)
    truth = ds.ground_truth
    labelled = set(zip(truth.table, truth.record_id))
    assert set(zip(v.table_name, v.record_id)) == labelled
    res = compare(v, truth)
    assert res["unlabelled_flags"] == []
    assert res["caught_by_expected_check"] == res["labelled_cases"] == len(truth)


def test_each_labelled_type_trips_its_own_check_and_a_declared_set_of_others(ds, injected):
    v = all_violations(run_checks(injected))
    for it, grp in ds.ground_truth.groupby("issue_type"):
        got = v[v.record_id.isin(grp.record_id)]
        assert EXPECTED_CHECK[it] in set(got.check_id), it
    # nothing outside the five expected checks plus the derived-value checks that see the same record
    assert set(v.check_id) <= set(EXPECTED_CHECK.values()) | {"FX_RECOMPUTE"}


def test_structural_checks_stay_silent_on_injected_data(injected):
    structural = {"PK_UNIQUENESS", "FK_INTEGRITY", "ALLOWED_ENUMS", "ID_FORMAT", "LIFECYCLE_TRANSITIONS"}
    for o in run_checks(injected):
        if o.category in structural:
            assert o.n_violations == 0, o.check_id


def test_validation_does_not_read_ground_truth():
    for f in ("checks", "report", "__init__", "__main__", "reconcile"):
        tree = ast.parse((ROOT / "src" / "validate" / f"{f}.py").read_text())
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        mods = {m for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for m in (n.module or "").split(".")}
        assert not ({"anomalies", "inject_anomalies", "expected_decision", "injected_changes"} & (names | mods)), f
        if f != "__main__":                      # the CLI reads it only behind --compare-ground-truth
            assert not any("ground_truth" in s for s in strings), f


def test_cli_output_is_identical_with_or_without_ground_truth_files(tmp_path):
    out = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(out)])
    assert validate_main(["--data", str(out)]) == 1
    first = (out / "validation" / "validation_summary.json").read_bytes()
    (out / "ground_truth" / "ground_truth.csv").write_text("garbage\n")
    validate_main(["--data", str(out)])
    assert (out / "validation" / "validation_summary.json").read_bytes() == first


# ---- 1. PK uniqueness --------------------------------------------------------------------------
def test_duplicate_primary_keys_are_flagged_in_each_kind_of_table(base):
    t = mutate(base, ledger_entry=lambda d: pd.concat([d, d.iloc[[3]]], ignore_index=True),
               daily_cash_position=lambda d: pd.concat([d, d.iloc[[5]]], ignore_index=True),
               receivable_status_event=lambda d: pd.concat([d, d.iloc[[2]]], ignore_index=True))
    got = flagged(t, "PK_UNIQUE")
    assert ("ledger_entry", base["ledger_entry"].ledger_entry_id.iloc[3]) in got
    assert any(tb == "daily_cash_position" for tb, _ in got) and any(tb == "receivable_status_event" for tb, _ in got)


def test_unique_constraint_violation_is_flagged(base):
    sl = base["settlement_link"]
    t = mutate(base, settlement_link=lambda d: pd.concat([d, d.iloc[[0]].assign(settlement_link_id=10**6)], ignore_index=True))
    assert any(tb == "settlement_link" for tb, _ in flagged(t, "PK_UNIQUE"))


def test_missing_required_value_is_flagged(base):
    t = mutate(base, receivable=lambda d: d.assign(currency=d.currency.where(d.index != 4)))
    assert ("receivable", base["receivable"].receivable_id.iloc[4]) in flagged(t, "NOT_NULL")


# ---- 2. FK integrity ----------------------------------------------------------------------------
def test_dangling_foreign_keys_are_flagged(base):
    t = mutate(base, ledger_entry=lambda d: d.assign(bank_account_id=d.bank_account_id.where(d.index != 7, "BANKACC-ENT-999999-001")),
               cash_flow_forecast=lambda d: d.assign(forecast_run_id=d.forecast_run_id.where(d.index != 2, "FCAST-2030-01-01-001")))
    got = flagged(t, "FK_EXISTS")
    assert ("ledger_entry", base["ledger_entry"].ledger_entry_id.iloc[7]) in got
    assert ("cash_flow_forecast", base["cash_flow_forecast"].cash_flow_forecast_id.iloc[2]) in got


def test_settlement_link_needs_exactly_one_parent(base):
    t = mutate(base, settlement_link=lambda d: d.assign(payable_id=d.payable_id.where(d.index != 0, "PAY-2025-000001"),
                                                        receivable_id=d.receivable_id.where(d.index != 0, "RCV-2025-000001")))
    assert flagged(t, "FK_SETTLEMENT_XOR") == {("settlement_link", str(base["settlement_link"].settlement_link_id.iloc[0]))}


# ---- 3. enums -----------------------------------------------------------------------------------
def test_values_outside_the_schema_enums_are_flagged(base):
    t = mutate(base, receivable=lambda d: d.assign(status=d.status.where(d.index != 1, "PENDING")),
               ledger_entry=lambda d: d.assign(direction=d.direction.where(d.index != 2, "DR")))
    got = flagged(t, "ENUM_VALUES")
    assert ("receivable", base["receivable"].receivable_id.iloc[1]) in got
    assert ("ledger_entry", base["ledger_entry"].ledger_entry_id.iloc[2]) in got


def test_numeric_enums_from_csv_floats_are_not_false_alarms(base):
    t = mutate(base, payable=lambda d: d.assign(is_one_off=d.is_one_off.astype(float)))
    assert flagged(t, "ENUM_VALUES") == set()


def test_numeric_enum_violations_are_flagged(base):
    p = base["payable"]
    t = mutate(base, payable=lambda d: d.assign(is_one_off=d.is_one_off.where(d.index != 0, 7)))
    assert ("payable", p.payable_id.iloc[0]) in flagged(t, "ENUM_VALUES")


def test_a_check_that_crashes_is_reported_as_a_failure(base, monkeypatch):
    def boom(t):
        raise RuntimeError("boom")
    monkeypatch.setattr(C, "REGISTRY", [C.Check("EXPLODES", "PK_UNIQUENESS", "always raises", boom)])
    (out,) = C.run_checks(base)
    assert out.n_violations == 1 and "boom" in out.violations.detail.iloc[0] and out.violations.check_id.iloc[0] == "EXPLODES"


def test_enum_values_are_read_from_the_schema_file():
    s = C.schema()
    assert s.enums["receivable"]["status"] == {"OPEN", "PARTIALLY_SETTLED", "OVERDUE", "SETTLED", "WRITTEN_OFF", "CANCELLED"}
    assert s.enums["ledger_entry"]["direction"] == {"CREDIT", "DEBIT"}
    assert "NOT" not in s.enums["ledger_entry"]
    assert s.pk["daily_cash_position"] == ["bank_account_id", "position_date"]
    assert ("bank_account_id", "bank_account", "bank_account_id") in s.fks["ledger_entry"]


# ---- 4. IDs --------------------------------------------------------------------------------------
@pytest.mark.parametrize("table,col,bad", [
    ("ledger_entry", "ledger_entry_id", "LED-2026-123"), ("legal_entity", "entity_id", "ENT-12"),
    ("bank_account", "bank_account_id", "BANKACC-ENT-000001-1"), ("receivable", "receivable_id", "rcv-2026-000001"),
    ("forecast_run", "forecast_run_id", "FCAST-2026-1-1-001"), ("cash_flow_forecast", "cash_flow_forecast_id", "CF-26-000001"),
    ("currency_rate", "rate_id", "FXR-EU-20260101"),
    ("ledger_entry", "ledger_entry_id", "LED-2026-00000001X"), ("legal_entity", "entity_id", "ENT-0000012"),
    ("receivable", "receivable_id", "xRCV-2026-000001")])
def test_malformed_ids_are_flagged(base, table, col, bad):
    t = mutate(base, **{table: lambda d: d.assign(**{col: d[col].where(d.index != 0, bad)})})
    assert any(tb == table and rid == bad for tb, rid in flagged(t, "ID_REGEX"))


def test_inconsistent_id_parts_are_flagged(base):
    led = base["ledger_entry"]
    wrong_year = led.ledger_entry_id.iloc[0].replace("2026", "2025")
    t = mutate(base, ledger_entry=lambda d: d.assign(ledger_entry_id=d.ledger_entry_id.where(d.index != 0, wrong_year)))
    assert ("ledger_entry", wrong_year) in flagged(t, "ID_CONSISTENT")
    acct = base["bank_account"].bank_account_id.iloc[0]
    other = acct.replace("ENT-0000", "ENT-0099")
    t = mutate(base, bank_account=lambda d: d.assign(bank_account_id=d.bank_account_id.where(d.index != 0, other)))
    assert ("bank_account", other) in flagged(t, "ID_CONSISTENT")


# ---- 5. dates ------------------------------------------------------------------------------------
def test_impossible_and_wrongly_formatted_dates_are_flagged(base):
    t = mutate(base, ledger_entry=lambda d: d.assign(entry_date=d.entry_date.where(d.index != 0, "2026-02-30")
                                                    .where(d.index != 1, "01/02/2026")))
    got = flagged(t, "DATE_FORMAT")
    assert len(got) == 2


def test_document_dates_out_of_order_are_flagged(base):
    r = base["receivable"]
    t = mutate(base, receivable=lambda d: d.assign(due_date=d.due_date.where(d.index != 0, "2000-01-01")))
    assert ("receivable", r.receivable_id.iloc[0]) in flagged(t, "DATE_PAIRS")
    t = mutate(base, forecast_run=lambda d: d.assign(horizon_end_date=d.horizon_end_date.where(d.index != 0, d.cutoff_date)))
    assert ("forecast_run", base["forecast_run"].forecast_run_id.iloc[0]) in flagged(t, "DATE_PAIRS")


def test_forecast_item_using_later_data_is_flagged_by_each_of_its_three_sources(base):
    cf, run = base["cash_flow_forecast"], base["forecast_run"].set_index("forecast_run_id")
    late = (pd.Timestamp(run.cutoff_date.max()) + pd.Timedelta(days=60)).strftime("%Y-%m-%d")
    t = mutate(base, cash_flow_forecast=lambda d: d.assign(source_data_date=d.source_data_date.where(d.index != 0, late)))
    assert ("cash_flow_forecast", cf.cash_flow_forecast_id.iloc[0]) in flagged(t, "DATE_CUTOFF")
    led = base["ledger_entry"]
    after = led[led.entry_date > run.cutoff_date.min()].ledger_entry_id.iloc[0]
    i = cf.index[cf.forecast_run_id == run.cutoff_date.idxmin()][1]
    t = mutate(base, cash_flow_forecast=lambda d: d.assign(source_ledger_entry_id=d.source_ledger_entry_id.where(d.index != i, after)))
    assert ("cash_flow_forecast", cf.cash_flow_forecast_id[i]) in flagged(t, "DATE_CUTOFF")
    rates = base["currency_rate"]
    j = cf.index[cf.forecast_run_id == run.cutoff_date.idxmin()][2]
    later = rates[rates.rate_date > run.cutoff_date.min()].rate_id.iloc[0]
    t = mutate(base, cash_flow_forecast=lambda d: d.assign(fx_rate_id=d.fx_rate_id.where(d.index != j, later)))
    assert ("cash_flow_forecast", cf.cash_flow_forecast_id[j]) in flagged(t, "DATE_CUTOFF")


def test_events_out_of_date_order_or_starting_off_invoice_date_are_flagged(base):
    ev = base["receivable_status_event"]
    rid = ev.receivable_id[ev.event_seq == 2].iloc[0]
    row = ev[(ev.receivable_id == rid) & (ev.event_seq == 2)].index[0]
    t = mutate(base, receivable_status_event=lambda d: d.assign(event_date=d.event_date.where(d.index != row, "1999-01-01")))
    assert flagged(t, "DATE_EVENTS")
    first = ev[ev.event_seq == 1].index[0]
    t = mutate(base, receivable_status_event=lambda d: d.assign(event_date=d.event_date.where(d.index != first, "1999-01-01")))
    assert ("receivable_status_event", str(ev.event_id[first])) in flagged(t, "DATE_EVENTS")


def test_settling_before_the_invoice_is_flagged(base):
    sl = base["settlement_link"]
    row = sl[sl.receivable_id.notna()].iloc[0]
    led = base["ledger_entry"]
    i = led.index[led.ledger_entry_id == row.ledger_entry_id][0]
    t = mutate(base, ledger_entry=lambda d: d.assign(entry_date=d.entry_date.where(d.index != i, "1990-01-01")))
    assert ("settlement_link", str(row.settlement_link_id)) in flagged(t, "DATE_SETTLEMENT")


def test_comparing_against_an_actual_on_or_before_the_cutoff_is_flagged(base):
    v = base["forecast_vs_actual"]
    run = base["forecast_run"].set_index("forecast_run_id").cutoff_date
    t = mutate(base, forecast_vs_actual=lambda d: d.assign(actual_date=d.actual_date.where(d.index != 0, run[d.forecast_run_id.iloc[0]])))
    assert ("forecast_vs_actual", v.comparison_id.iloc[0]) in flagged(t, "DATE_ACTUALS_AFTER_CUTOFF")


# ---- 6. arithmetic ----------------------------------------------------------------------------------
def test_position_that_does_not_reconcile_is_flagged_three_ways(base):
    p = base["daily_cash_position"].sort_values(["bank_account_id", "position_date"]).reset_index(drop=True)
    for col in ("closing_minor", "opening_minor", "total_credits_minor"):
        t = dict(base)
        t["daily_cash_position"] = p.copy()
        t["daily_cash_position"].loc[20, col] += 1
        key = f"{p.bank_account_id[20]}|{p.position_date[20]}"
        assert ("daily_cash_position", key) in flagged(t, "POSITION_RECONCILES"), col


def test_wrong_base_amount_and_wrong_rate_are_flagged(base):
    led = base["ledger_entry"]
    i = led.index[led.currency != "USD"][0]
    t = mutate(base, ledger_entry=lambda d: d.assign(base_amount_minor=d.base_amount_minor.where(d.index != i, d.base_amount_minor + 5)))
    assert ("ledger_entry", led.ledger_entry_id[i]) in flagged(t, "FX_RECOMPUTE")
    other = base["currency_rate"]
    wrong = other[other.quote_currency != led.currency[i]].rate_id.iloc[0]
    t = mutate(base, ledger_entry=lambda d: d.assign(fx_rate_id=d.fx_rate_id.where(d.index != i, wrong)))
    assert ("ledger_entry", led.ledger_entry_id[i]) in flagged(t, "FX_RECOMPUTE")


def test_fx_completeness_flags_missing_rate_missing_base_and_stray_rate(base):
    led = base["ledger_entry"]
    foreign = led.index[led.currency != "USD"]
    usd = led.index[led.currency == "USD"][0]
    t = mutate(base, ledger_entry=lambda d: d.assign(fx_rate_id=d.fx_rate_id.where(d.index != foreign[0], None),
                                                    base_amount_minor=d.base_amount_minor.where(d.index != foreign[1], None)))
    got = flagged(t, "FX_COMPLETE")
    assert {("ledger_entry", led.ledger_entry_id[foreign[0]]), ("ledger_entry", led.ledger_entry_id[foreign[1]])} <= got
    t = mutate(base, ledger_entry=lambda d: d.assign(fx_rate_id=d.fx_rate_id.where(d.index != usd, "FXR-EUR-20260101")))
    assert ("ledger_entry", led.ledger_entry_id[usd]) in flagged(t, "FX_COMPLETE")


def test_settlement_allocation_errors_are_flagged(base):
    sl = base["settlement_link"]
    t = mutate(base, settlement_link=lambda d: d.assign(allocated_amount_minor=d.allocated_amount_minor.where(d.index != 0, d.allocated_amount_minor + 1)))
    assert ("ledger_entry", sl.ledger_entry_id.iloc[0]) in flagged(t, "SETTLEMENT_ALLOCATION")
    rid = sl.receivable_id.dropna().iloc[0]
    t = mutate(base, receivable=lambda d: d.assign(amount_minor=d.amount_minor.where(d.receivable_id != rid, 1)))
    assert ("receivable", rid) in flagged(t, "SETTLEMENT_ALLOCATION")


def test_projection_arithmetic_flag_and_chain_errors_are_flagged(base):
    p = base["forecast_daily_projection"].sort_values(["forecast_run_id", "entity_id", "projection_date"]).reset_index(drop=True)
    key = lambda i: f"{p.forecast_run_id[i]}|{p.entity_id[i]}|{p.projection_date[i]}"
    for col in ("projected_closing_base_minor", "projected_inflow_base_minor"):
        t = dict(base)
        t["forecast_daily_projection"] = p.copy()
        t["forecast_daily_projection"].loc[10, col] += 7
        assert ("forecast_daily_projection", key(10)) in flagged(t, "PROJECTION_ARITHMETIC"), col
    t = dict(base)
    q = p.copy()
    q.loc[10, "shortfall_flag"] = 1 - q.loc[10, "shortfall_flag"]
    t["forecast_daily_projection"] = q
    assert ("forecast_daily_projection", key(10)) in flagged(t, "PROJECTION_ARITHMETIC")


def test_variance_arithmetic_errors_are_flagged(base):
    v = base["forecast_vs_actual"]
    for col in ("variance_base_minor", "fx_variance_base_minor", "date_variance_days"):
        i = v.index[v[col].notna()][0]
        t = mutate(base, forecast_vs_actual=lambda d: d.assign(**{col: d[col].where(d.index != i, d[col] + 3)}))
        assert ("forecast_vs_actual", v.comparison_id[i]) in flagged(t, "VARIANCE_ARITHMETIC"), col


def test_reversal_that_does_not_mirror_is_flagged(base):
    led = base["ledger_entry"]
    rev = led[led.reverses_ledger_entry_id.notna()].index[0]
    t = mutate(base, ledger_entry=lambda d: d.assign(amount_minor=d.amount_minor.where(d.index != rev, d.amount_minor + 1)))
    assert ("ledger_entry", led.ledger_entry_id[rev]) in flagged(t, "REVERSAL_MIRRORS")


# ---- 7. lifecycle -----------------------------------------------------------------------------------
def test_illegal_transition_is_flagged_and_so_is_a_stored_status_that_disagrees(base):
    ev = base["receivable_status_event"]
    row = ev[ev.to_status == "SETTLED"].index[0]
    t = mutate(base, receivable_status_event=lambda d: d.assign(to_status=d.to_status.where(d.index != row, "CANCELLED")))
    assert ("receivable_status_event", str(ev.event_id[row])) in flagged(t, "LIFECYCLE_RECEIVABLE")
    r = base["receivable"]
    k = r.index[r.status != "WRITTEN_OFF"][0]
    t = mutate(base, receivable=lambda d: d.assign(status=d.status.where(d.index != k, "WRITTEN_OFF")))
    assert ("receivable", r.receivable_id[k]) in flagged(t, "LIFECYCLE_RECEIVABLE")


def test_leaving_a_terminal_state_and_sequence_gaps_are_flagged(base):
    ev = base["payable_status_event"]
    done = ev[ev.to_status == "SETTLED"].iloc[0]
    extra = pd.DataFrame([{**done.to_dict(), "event_id": 10**6, "event_seq": done.event_seq + 1,
                           "from_status": "SETTLED", "to_status": "OPEN", "cause": "CREATED"}])
    t = mutate(base, payable_status_event=lambda d: pd.concat([d, extra], ignore_index=True))
    assert ("payable_status_event", "1000000") in flagged(t, "LIFECYCLE_PAYABLE")
    gap = ev[ev.event_seq == 2].index[0]
    t = mutate(base, payable_status_event=lambda d: d.assign(event_seq=d.event_seq.where(d.index != gap, 9)))
    assert ("payable_status_event", str(ev.event_id[gap])) in flagged(t, "LIFECYCLE_PAYABLE")


def test_run_lifecycle_and_supersession_are_checked(base):
    ev = base["forecast_run_status_event"]
    row = ev[ev.to_status == "COMPLETED"].index[0]
    t = mutate(base, forecast_run_status_event=lambda d: d.assign(to_status=d.to_status.where(d.index != row, "CREATED")))
    assert ("forecast_run_status_event", str(ev.event_id[row])) in flagged(t, "LIFECYCLE_RUN")
    fr = base["forecast_run"]
    sup = fr[fr.status == "SUPERSEDED"].index[0]
    t = mutate(base, forecast_run=lambda d: d.assign(status=d.status.where(d.index != sup, "APPROVED")))
    assert ("forecast_run", fr.forecast_run_id[sup]) in flagged(t, "LIFECYCLE_RUN")


def test_settled_document_must_be_fully_allocated(base):
    r = base["receivable"]
    sl = base["settlement_link"]
    done = r[(r.status == "SETTLED")].receivable_id.iloc[0]
    t = mutate(base, receivable=lambda d: d.assign(amount_minor=d.amount_minor.where(d.receivable_id != done, d.amount_minor + 100)))
    assert ("receivable", done) in flagged(t, "LIFECYCLE_SETTLEMENT_STATE")


def test_validator_lifecycle_tables_match_the_generators():
    assert C.RECEIVABLE_TRANSITIONS == {None: {"OPEN"}, **txn_events.ALLOWED["R"]}
    assert C.PAYABLE_TRANSITIONS == {None: {"OPEN"}, **txn_events.ALLOWED["P"]}
    assert C.RUN_TRANSITIONS == forecast_runs.RUN_TRANSITIONS


# ---- 8. duplicates ---------------------------------------------------------------------------------
def test_repeated_forecast_item_is_flagged_as_the_repeat_only(base):
    cf = base["cash_flow_forecast"]
    dup = cf.iloc[[10]].assign(cash_flow_forecast_id="CF-2026-999999")
    t = mutate(base, cash_flow_forecast=lambda d: pd.concat([d, dup], ignore_index=True))
    assert flagged(t, "DUP_FORECAST_ITEM") == {("cash_flow_forecast", "CF-2026-999999")}


def test_forecast_items_that_differ_only_in_run_are_not_duplicates(base):
    # a re-run legitimately repeats items under another run id
    cf = base["cash_flow_forecast"]
    cols = [c for c in cf.columns if c not in ("cash_flow_forecast_id", "forecast_run_id")]
    assert cf.duplicated(subset=cols).any()
    assert flagged(base, "DUP_FORECAST_ITEM") == set()


def test_reused_bank_reference_and_repeated_document_are_flagged(base):
    led = base["ledger_entry"]
    t = mutate(base, ledger_entry=lambda d: d.assign(external_ref=d.external_ref.where(d.index != 5, led.external_ref.iloc[0])))
    assert ("ledger_entry", led.ledger_entry_id.iloc[5]) in flagged(t, "DUP_LEDGER_REF")
    r = base["receivable"]
    dup = r.iloc[[3]].assign(receivable_id="RCV-2026-999999")
    t = mutate(base, receivable=lambda d: pd.concat([d, dup], ignore_index=True))
    assert ("receivable", "RCV-2026-999999") in flagged(t, "DUP_DOCUMENT")


def test_repeated_settlement_link_is_flagged(base):
    sl = base["settlement_link"]
    t = mutate(base, settlement_link=lambda d: pd.concat([d, d.iloc[[0]].assign(settlement_link_id=10**6)], ignore_index=True))
    assert ("settlement_link", "1000000") in flagged(t, "DUP_SETTLEMENT_LINK")


# ---- 9. currency -----------------------------------------------------------------------------------
def test_currency_that_differs_from_the_account_is_flagged_in_every_table(base):
    t = mutate(base, ledger_entry=lambda d: d.assign(currency=d.currency.where(d.index != 0, "JPY")),
               receivable=lambda d: d.assign(currency=d.currency.where(d.index != 0, "JPY")),
               payable=lambda d: d.assign(currency=d.currency.where(d.index != 0, "JPY")),
               cash_flow_forecast=lambda d: d.assign(currency=d.currency.where(d.index != 0, "JPY")))
    got = {tb for tb, _ in flagged(t, "CURRENCY_ACCOUNT")}
    expected = {tb for tb in ("ledger_entry", "receivable", "payable", "cash_flow_forecast")
                if base[tb].currency.iloc[0] != "JPY"}
    assert got == expected


def test_unknown_currency_code_and_bad_rate_are_flagged(base):
    t = mutate(base, ledger_entry=lambda d: d.assign(currency=d.currency.where(d.index != 0, "XXX")),
               currency_rate=lambda d: d.assign(rate=d.rate.where(d.index != 0, -1.0)))
    got = flagged(t, "CURRENCY_CODES")
    assert ("ledger_entry", base["ledger_entry"].ledger_entry_id.iloc[0]) in got
    assert ("currency_rate", base["currency_rate"].rate_id.iloc[0]) in got


# ---- reports --------------------------------------------------------------------------------------
def test_summary_counts_add_up_and_report_names_failures(injected, ds):
    outcomes = run_checks(injected)
    s = summary(outcomes, injected, "x")
    assert s["status"] == "FAIL" and s["violations"] == sum(o.n_violations for o in outcomes)
    assert sum(c["violations"] for c in s["by_category"].values()) == s["violations"]
    assert sum(c["checks"] for c in s["by_category"].values()) == s["checks_run"] == len(REGISTRY)
    assert s["row_counts"]["ledger_entry"] == len(injected["ledger_entry"])
    md = report_markdown(s, outcomes, compare(all_violations(outcomes), ds.ground_truth))
    assert "Overall: FAIL" in md and "not labelled: **0**" in md and "DATE_CUTOFF" in md


def test_clean_summary_is_pass(base):
    outcomes = run_checks(base)
    s = summary(outcomes, base)
    assert s["status"] == "PASS" and s["violations"] == 0 and s["checks_failed"] == 0
    assert "this dataset is valid" in report_markdown(s, outcomes)


def test_cli_baseline_exits_zero_and_injected_exits_one(tmp_path):
    clean, dirty = tmp_path / "clean", tmp_path / "dirty"
    generate_main(["--scale", "starter", "--out", str(clean), "--no-anomalies"])
    generate_main(["--scale", "starter", "--out", str(dirty)])
    assert validate_main(["--data", str(clean)]) == 0
    assert validate_main(["--data", str(dirty), "--compare-ground-truth"]) == 1
    s = json.loads((dirty / "validation" / "validation_summary.json").read_text())
    assert s["ground_truth_comparison"]["unlabelled_flags"] == []
    assert json.loads((clean / "validation" / "validation_summary.json").read_text())["status"] == "PASS"
    assert (dirty / "validation" / "validation_report.md").read_text().startswith("# Validation report")
    assert pd.read_csv(dirty / "validation" / "validation_violations.csv").shape[0] == s["violations"]


def test_csv_round_trip_gives_the_same_results_as_memory(tmp_path, injected):
    out = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(out)])
    a = all_violations(run_checks(injected))
    b = all_violations(run_checks(load_all(out)))
    assert set(zip(a.check_id, a.record_id)) == set(zip(b.check_id, b.record_id))


def test_every_check_is_documented():
    doc = (ROOT / "docs" / "validation.md").read_text()
    missing = [c.check_id for c in REGISTRY if f"`{c.check_id}`" not in doc]
    assert not missing, missing


def test_posting_after_suspension_or_closure_is_flagged(base):
    ent = base["legal_entity"]
    susp = ent[ent.status == "SUSPENDED"].iloc[0]
    led, acc = base["ledger_entry"], base["bank_account"]
    mine = acc[acc.entity_id == susp.entity_id].bank_account_id.iloc[0]
    i = led.index[led.bank_account_id == mine][0]
    t = mutate(base, ledger_entry=lambda d: d.assign(entry_date=d.entry_date.where(d.index != i, susp.suspended_from)))
    assert ("ledger_entry", led.ledger_entry_id[i]) in flagged(t, "LIFECYCLE_ACTIVITY_WINDOW")
    closed = acc[acc.effective_to.notna()].iloc[0]
    j = led.index[led.bank_account_id == closed.bank_account_id][0]
    after = (pd.Timestamp(closed.effective_to) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    t = mutate(base, ledger_entry=lambda d: d.assign(entry_date=d.entry_date.where(d.index != j, after)))
    assert ("ledger_entry", led.ledger_entry_id[j]) in flagged(t, "LIFECYCLE_ACTIVITY_WINDOW")


def test_suspended_before_it_began_is_flagged(base):
    ent = base["legal_entity"]
    k = ent.index[ent.status == "SUSPENDED"][0]
    t = mutate(base, legal_entity=lambda d: d.assign(suspended_from=d.suspended_from.where(d.index != k, "1999-01-01")))
    assert ("legal_entity", ent.entity_id[k]) in flagged(t, "DATE_PAIRS")
