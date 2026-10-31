"""Schema tests.

Three jobs:
1. A small hand-built, internally consistent dataset loads with foreign keys on
   and every validation view is empty.
2. Structural rules are rejected at insert; the five labelled anomalies are
   NOT rejected (they must load) but ARE flagged by the validation views.
3. docs/data_dictionary.md and sql/schema.sql list exactly the same columns.
"""
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "sql" / "schema.sql").read_text()
DICTIONARY = (ROOT / "docs" / "data_dictionary.md").read_text()

CUTOFF = "2026-01-03"
EUR_ACCT = "BANKACC-ENT-000001-002"


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def ins(conn, table, row):
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def rows(conn, sql):
    return conn.execute(sql).fetchall()


# row factories: each returns a valid row with a fresh primary key
def entity(**kw):
    r = dict(entity_id="ENT-000003", legal_name="Gamma Test Ltd", country_code="US",
             functional_currency="USD", entity_type="OPERATING", parent_entity_id=None,
             min_liquidity_buffer_base_minor=100000, status="ACTIVE",
             effective_from="2026-01-01", effective_to=None, suspended_from=None)
    return {**r, **kw}


def account(**kw):
    r = dict(bank_account_id="BANKACC-ENT-000002-002", entity_id="ENT-000002", account_seq=2,
             bank_name="Test Bank", account_type="OPERATING", currency="EUR",
             opening_balance_minor=1000, opening_date="2026-01-01", status="ACTIVE",
             effective_from="2026-01-01", effective_to=None)
    return {**r, **kw}


def ledger(**kw):
    r = dict(ledger_entry_id="LED-2026-00000010", bank_account_id=EUR_ACCT,
             entry_date="2026-01-02", direction="CREDIT", amount_minor=1000, currency="EUR",
             fx_rate_id="FXR-EUR-20260102", base_amount_minor=1100,
             entry_type="OPERATING_INFLOW", external_ref="X10", narrative=None,
             reverses_ledger_entry_id=None)
    return {**r, **kw}


def receivable(**kw):
    r = dict(receivable_id="RCV-2026-000002", entity_id="ENT-000001", bank_account_id=EUR_ACCT,
             counterparty_name="Acme", counterparty_risk_tier="LOW", document_ref="INV-2",
             currency="EUR", amount_minor=5000, invoice_date="2026-01-01",
             due_date="2026-01-10", expected_date="2026-01-05", status="OPEN")
    return {**r, **kw}


def link(**kw):
    r = dict(receivable_id="RCV-2026-000002", payable_id=None,
             ledger_entry_id="LED-2026-00000002", allocated_amount_minor=100)
    return {**r, **kw}


def rcv_event(**kw):
    r = dict(receivable_id="RCV-2026-000002", event_seq=1, from_status=None, to_status="OPEN",
             event_date="2026-01-01", cause="CREATED", ledger_entry_id=None, actor_role="SYSTEM")
    return {**r, **kw}


def scenario(**kw):
    r = dict(scenario_id="SCN-004", scenario_code="STRESS", version_no=2, scenario_name="Stress v2",
             assumption_notes="test", receipt_delay_days=0, collection_haircut_bps=500,
             fx_shock_bps=0, one_off_multiplier_bps=10000, status="ACTIVE",
             effective_from="2026-01-01", effective_to=None)
    return {**r, **kw}


def run(**kw):
    r = dict(forecast_run_id="FCAST-2026-01-04-001", scenario_id="SCN-001", cutoff_date="2026-01-04",
             run_seq=1, version_no=1, supersedes_run_id=None, horizon_days=7,
             horizon_end_date="2026-01-11", base_currency="USD", status="CREATED",
             scenario_params_json="{}", run_checksum=None)
    return {**r, **kw}


def cff(**kw):
    r = dict(cash_flow_forecast_id="CF-2026-000010", forecast_run_id="FCAST-2026-01-03-001",
             bank_account_id=EUR_ACCT, source_type="RUN_RATE", receivable_id=None, payable_id=None,
             source_ledger_entry_id=None, source_data_date="2026-01-03", direction="CREDIT",
             currency="EUR", expected_date="2026-01-05", forecast_date="2026-01-05",
             unadjusted_amount_minor=5000, forecast_amount_minor=5000,
             fx_rate_id="FXR-EUR-20260103", forecast_base_amount_minor=5500)
    return {**r, **kw}


def projection(**kw):
    r = dict(forecast_run_id="FCAST-2026-01-03-001", entity_id="ENT-000002",
             projection_date="2026-01-05", opening_base_minor=1000, projected_inflow_base_minor=0,
             projected_outflow_base_minor=0, projected_closing_base_minor=1000,
             liquidity_buffer_base_minor=100, shortfall_flag=0)
    return {**r, **kw}


def comparison(**kw):
    r = dict(comparison_id="VAR-2026-000002", forecast_run_id="FCAST-2026-01-03-001",
             cash_flow_forecast_id="CF-2026-000001", actual_ledger_entry_id=None,
             compared_through_date="2026-01-09", direction="CREDIT", currency="EUR",
             forecast_date="2026-01-05", actual_date=None, date_variance_days=None,
             forecast_amount_minor=5000, actual_amount_minor=0, forecast_net_base_minor=5500,
             actual_net_base_minor=0, variance_base_minor=-5500, amount_variance_base_minor=-5500,
             fx_variance_base_minor=0, variance_driver="NOT_YET_SETTLED")
    return {**r, **kw}


def position(day, opening, credits, debits, closing, base, **kw):
    r = dict(bank_account_id=EUR_ACCT, position_date=day, currency="EUR", opening_minor=opening,
             total_credits_minor=credits, total_debits_minor=debits, closing_minor=closing,
             entry_count=0, fx_rate_id=f"FXR-EUR-{day.replace('-', '')}", closing_base_minor=base)
    return {**r, **kw}


# ----------------------------------------------------------------------------
# fixture: a small consistent dataset
# ----------------------------------------------------------------------------
@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")

    for code, name, exp, base in [("USD", "US Dollar", 2, 1), ("EUR", "Euro", 2, 0),
                                  ("JPY", "Yen", 0, 0)]:
        ins(conn, "currency", dict(currency_code=code, currency_name=name,
                                   minor_unit_exponent=exp, is_base=base))

    ins(conn, "legal_entity", entity(entity_id="ENT-000001", legal_name="Alpha Test Ltd"))
    ins(conn, "legal_entity", entity(entity_id="ENT-000002", legal_name="Beta Test GmbH",
                                     country_code="DE", functional_currency="EUR"))
    ins(conn, "bank_account", account(bank_account_id="BANKACC-ENT-000001-001", entity_id="ENT-000001",
                                      account_seq=1, currency="USD", opening_balance_minor=1000000))
    ins(conn, "bank_account", account(bank_account_id=EUR_ACCT, entity_id="ENT-000001",
                                      account_seq=2, currency="EUR", opening_balance_minor=500000))
    ins(conn, "bank_account", account(bank_account_id="BANKACC-ENT-000002-001", entity_id="ENT-000002",
                                      account_seq=1, currency="EUR", opening_balance_minor=1000))

    for d in ("20260101", "20260102", "20260103", "20260104"):
        ins(conn, "currency_rate", dict(
            rate_id=f"FXR-EUR-{d}", rate_date=f"{d[:4]}-{d[4:6]}-{d[6:]}", quote_currency="EUR",
            base_currency="USD", rate=1.1, rate_source="SYNTHETIC_RANDOM_WALK"))

    ins(conn, "receivable", receivable(receivable_id="RCV-2026-000001", document_ref="INV-1",
                                       amount_minor=100000, expected_date="2026-01-02",
                                       status="SETTLED"))
    ins(conn, "payable", dict(
        payable_id="PAY-2026-000001", entity_id="ENT-000001", bank_account_id=EUR_ACCT,
        payee_name="Supplier", payable_category="TRADE", is_one_off=0, document_ref="BILL-1",
        currency="EUR", amount_minor=30000, invoice_date="2026-01-01", due_date="2026-01-10",
        expected_date="2026-01-03", status="SETTLED"))

    ins(conn, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000001", amount_minor=100000,
                                     base_amount_minor=110000, entry_type="RECEIPT_SETTLEMENT",
                                     external_ref="R1"))
    ins(conn, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000002", entry_date="2026-01-03",
                                     direction="DEBIT", amount_minor=30000, base_amount_minor=33000,
                                     fx_rate_id="FXR-EUR-20260103", entry_type="PAYMENT_SETTLEMENT",
                                     external_ref="P1"))
    ins(conn, "settlement_link", link(receivable_id="RCV-2026-000001",
                                      ledger_entry_id="LED-2026-00000001",
                                      allocated_amount_minor=100000))
    ins(conn, "settlement_link", link(receivable_id=None, payable_id="PAY-2026-000001",
                                      ledger_entry_id="LED-2026-00000002",
                                      allocated_amount_minor=30000))

    ins(conn, "receivable_status_event", rcv_event(receivable_id="RCV-2026-000001"))
    ins(conn, "receivable_status_event", rcv_event(
        receivable_id="RCV-2026-000001", event_seq=2, from_status="OPEN", to_status="SETTLED",
        event_date="2026-01-02", cause="FULL_SETTLEMENT", ledger_entry_id="LED-2026-00000001"))
    ins(conn, "payable_status_event", dict(
        payable_id="PAY-2026-000001", event_seq=1, from_status=None, to_status="OPEN",
        event_date="2026-01-01", cause="CREATED", ledger_entry_id=None, actor_role="SYSTEM"))

    # EUR account: open 500000; +100000 on 01-02; -30000 on 01-03
    ins(conn, "daily_cash_position", position("2026-01-01", 500000, 0, 0, 500000, 550000))
    ins(conn, "daily_cash_position", position("2026-01-02", 500000, 100000, 0, 600000, 660000,
                                              entry_count=1))
    ins(conn, "daily_cash_position", position("2026-01-03", 600000, 0, 30000, 570000, 627000,
                                              entry_count=1))

    ins(conn, "liquidity_scenario", scenario(scenario_id="SCN-001", scenario_code="BASE",
                                             version_no=1, scenario_name="Base",
                                             collection_haircut_bps=0))
    ins(conn, "liquidity_scenario", scenario(scenario_id="SCN-002", scenario_code="DELAYED_RECEIPT",
                                             version_no=1, scenario_name="Delayed",
                                             receipt_delay_days=5, collection_haircut_bps=0))
    ins(conn, "liquidity_scenario", scenario(scenario_id="SCN-003", scenario_code="STRESS",
                                             version_no=1, scenario_name="Stress",
                                             collection_haircut_bps=1000, fx_shock_bps=-1000,
                                             one_off_multiplier_bps=15000))

    ins(conn, "forecast_run", run(forecast_run_id="FCAST-2026-01-03-001", cutoff_date=CUTOFF,
                                  horizon_end_date="2026-01-10", status="COMPLETED"))
    ins(conn, "forecast_run", run(forecast_run_id="FCAST-2026-01-03-002", cutoff_date=CUTOFF,
                                  run_seq=2, scenario_id="SCN-002", horizon_end_date="2026-01-10",
                                  status="COMPLETED"))
    ins(conn, "forecast_run_status_event", dict(
        forecast_run_id="FCAST-2026-01-03-001", event_seq=1, from_status=None, to_status="CREATED",
        event_date=CUTOFF, cause="CREATED", actor_role="SYSTEM"))

    ins(conn, "cash_flow_forecast", cff(cash_flow_forecast_id="CF-2026-000001"))
    ins(conn, "forecast_daily_projection", projection(
        entity_id="ENT-000001", opening_base_minor=627000, projected_inflow_base_minor=5500,
        projected_closing_base_minor=632500, liquidity_buffer_base_minor=100000))
    ins(conn, "forecast_vs_actual", comparison(comparison_id="VAR-2026-000001",
                                                 compared_through_date="2026-01-08"))

    conn.commit()
    return conn


# ----------------------------------------------------------------------------
# 1. clean data
# ----------------------------------------------------------------------------
VALIDATION_VIEWS = ["v_daily_position_arithmetic_breaks", "v_daily_position_chain_breaks",
                    "v_position_vs_ledger_breaks", "v_forecast_cutoff_violations",
                    "v_currency_integrity_issues"]


def test_clean_dataset_has_valid_foreign_keys(db):
    assert rows(db, "PRAGMA foreign_key_check") == []


@pytest.mark.parametrize("view", VALIDATION_VIEWS)
def test_validation_views_empty_on_clean_data(db, view):
    assert rows(db, f"SELECT * FROM {view}") == []


def test_settlement_views_reconcile_on_clean_data(db):
    assert rows(db, "SELECT outstanding_minor FROM v_receivable_settlement "
                    "WHERE receivable_id='RCV-2026-000001'") == [(0,)]
    assert rows(db, "SELECT outstanding_minor FROM v_payable_settlement") == [(0,)]


def test_entity_position_view_sums_base_amounts(db):
    got = rows(db, "SELECT closing_base_minor, account_count, accounts_missing_fx "
                   "FROM v_daily_entity_position WHERE entity_id='ENT-000001' "
                   "AND position_date='2026-01-03'")
    assert got == [(627000, 1, 0)]


# ----------------------------------------------------------------------------
# 2a. structural rules are rejected at insert
# ----------------------------------------------------------------------------
REJECTED = {
    "entity id format": ("legal_entity", entity(entity_id="ENT-1")),
    "closed entity needs effective_to": ("legal_entity", entity(status="CLOSED")),
    "entity parent is self": ("legal_entity", entity(parent_entity_id="ENT-000003")),
    "account id must match entity + seq": ("bank_account", account(
        bank_account_id="BANKACC-ENT-000002-009")),
    "account unknown currency": ("bank_account", account(currency="XXX")),
    "ledger id year must match entry_date": ("ledger_entry", ledger(
        ledger_entry_id="LED-2025-00000010")),
    "receipt must be a credit": ("ledger_entry", ledger(
        entry_type="RECEIPT_SETTLEMENT", direction="DEBIT")),
    "payment must be a debit": ("ledger_entry", ledger(
        entry_type="PAYMENT_SETTLEMENT", direction="CREDIT")),
    "reversal needs reversed id": ("ledger_entry", ledger(entry_type="REVERSAL")),
    "non-reversal cannot name reversed id": ("ledger_entry", ledger(
        reverses_ledger_entry_id="LED-2026-00000001")),
    "ledger amount positive": ("ledger_entry", ledger(amount_minor=0)),
    "ledger bad calendar date": ("ledger_entry", ledger(entry_date="2026-02-30")),
    "ledger unknown account (FK)": ("ledger_entry", ledger(bank_account_id="BANKACC-ENT-000009-001")),
    "second base currency": ("currency", dict(currency_code="GBP", currency_name="Pound",
                                              minor_unit_exponent=2, is_base=1)),
    "fx rate id must match currency and date": ("currency_rate", dict(
        rate_id="FXR-EUR-20260110", rate_date="2026-01-11", quote_currency="EUR",
        base_currency="USD", rate=1.1, rate_source="SYNTHETIC_RANDOM_WALK")),
    "fx rate positive": ("currency_rate", dict(
        rate_id="FXR-EUR-20260110", rate_date="2026-01-10", quote_currency="EUR",
        base_currency="USD", rate=0, rate_source="SYNTHETIC_RANDOM_WALK")),
    "duplicate document_ref per entity": ("receivable", receivable(document_ref="INV-1")),
    "receivable due before invoice": ("receivable", receivable(due_date="2025-12-31")),
    "settlement link with no parent": ("settlement_link", link(receivable_id=None)),
    "settlement link with two parents": ("settlement_link", link(payable_id="PAY-2026-000001")),
    "first event cannot have from_status": ("receivable_status_event", rcv_event(from_status="OPEN",
                                                                               to_status="SETTLED")),
    "later event needs from_status": ("receivable_status_event", rcv_event(event_seq=2)),
    "event cannot be a no-op": ("receivable_status_event", rcv_event(
        receivable_id="RCV-2026-000001", event_seq=3, from_status="SETTLED", to_status="SETTLED")),
    "BASE scenario must be neutral": ("liquidity_scenario", scenario(
        scenario_code="BASE", version_no=9, receipt_delay_days=3, collection_haircut_bps=0)),
    "DELAYED_RECEIPT must delay": ("liquidity_scenario", scenario(
        scenario_code="DELAYED_RECEIPT", version_no=9, collection_haircut_bps=0,
        receipt_delay_days=0)),
    "DELAYED_RECEIPT changes timing only": ("liquidity_scenario", scenario(
        scenario_code="DELAYED_RECEIPT", version_no=9, collection_haircut_bps=100,
        receipt_delay_days=3)),
    "run id must match cutoff and seq": ("forecast_run", run(forecast_run_id="FCAST-2026-01-04-002")),
    "run horizon end must match": ("forecast_run", run(horizon_end_date="2026-01-12")),
    "run params must be json": ("forecast_run", run(scenario_params_json="not json")),
    "v1 run cannot supersede": ("forecast_run", run(supersedes_run_id="FCAST-2026-01-03-001")),
    "v2 run must supersede": ("forecast_run", run(version_no=2)),
    "duplicate scenario+cutoff+version": ("forecast_run", run(
        forecast_run_id="FCAST-2026-01-03-003", cutoff_date=CUTOFF, run_seq=3,
        horizon_end_date="2026-01-10")),
    "receivable item needs receivable_id": ("cash_flow_forecast", cff(source_type="RECEIVABLE")),
    "run-rate item cannot have a source": ("cash_flow_forecast", cff(receivable_id="RCV-2026-000001")),
    "receivable item must be a credit": ("cash_flow_forecast", cff(
        source_type="RECEIVABLE", receivable_id="RCV-2026-000001", direction="DEBIT")),
    "forecast date before expected date": ("cash_flow_forecast", cff(forecast_date="2026-01-04")),
    "projection closing identity": ("forecast_daily_projection", projection(
        projected_closing_base_minor=999)),
    "projection shortfall flag rule": ("forecast_daily_projection", projection(shortfall_flag=1)),
    "comparison needs a forecast or an actual": ("forecast_vs_actual", comparison(
        cash_flow_forecast_id=None)),
    "comparison variance identity": ("forecast_vs_actual", comparison(variance_base_minor=-1)),
    "comparison variance split": ("forecast_vs_actual", comparison(fx_variance_base_minor=7)),
    "comparison unknown driver": ("forecast_vs_actual", comparison(variance_driver="BECAUSE")),
}


@pytest.mark.parametrize("case", REJECTED)
def test_structural_violation_is_rejected(db, case):
    table, row = REJECTED[case]
    with pytest.raises(sqlite3.IntegrityError):
        ins(db, table, row)


def test_valid_factory_rows_do_load(db):
    """Guards the rejection tests: unmodified factory rows must be accepted."""
    ins(db, "legal_entity", entity())
    ins(db, "bank_account", account())
    ins(db, "ledger_entry", ledger())
    ins(db, "receivable", receivable())
    ins(db, "receivable_status_event", rcv_event())
    ins(db, "liquidity_scenario", scenario())
    ins(db, "forecast_run", run())
    ins(db, "cash_flow_forecast", cff())
    ins(db, "forecast_daily_projection", projection())
    ins(db, "forecast_vs_actual", comparison())
    assert rows(db, "PRAGMA foreign_key_check") == []


# ----------------------------------------------------------------------------
# 2b. append-only tables
# ----------------------------------------------------------------------------
APPEND_ONLY = [
    ("UPDATE ledger_entry SET narrative='x'", "append-only"),
    ("DELETE FROM ledger_entry WHERE ledger_entry_id='LED-2026-00000001'", "append-only"),
    ("UPDATE cash_flow_forecast SET forecast_amount_minor=1", "immutable"),
    ("DELETE FROM cash_flow_forecast", "immutable"),
    ("UPDATE receivable_status_event SET cause='CREATED'", "append-only"),
    ("DELETE FROM receivable_status_event", "append-only"),
    ("UPDATE payable_status_event SET cause='CREATED'", "append-only"),
    ("DELETE FROM payable_status_event", "append-only"),
    ("UPDATE forecast_run_status_event SET cause='CREATED'", "append-only"),
    ("DELETE FROM forecast_run_status_event", "append-only"),
]


@pytest.mark.parametrize("sql,word", APPEND_ONLY)
def test_history_tables_cannot_be_changed(db, sql, word):
    with pytest.raises(sqlite3.DatabaseError, match=word):
        db.execute(sql)


def test_current_state_columns_are_still_updatable(db):
    db.execute("UPDATE receivable SET status='OVERDUE' WHERE receivable_id='RCV-2026-000001'")
    db.execute("UPDATE forecast_run SET status='APPROVED' WHERE forecast_run_id='FCAST-2026-01-03-001'")


# ----------------------------------------------------------------------------
# 2c. the five labelled anomalies must load AND be detectable from data alone
# ----------------------------------------------------------------------------
def test_anomaly_closing_balance_arithmetic_break(db):
    ins(db, "daily_cash_position", position("2026-01-04", 570000, 0, 0, 570100, 627110))
    assert rows(db, "SELECT bank_account_id, position_date, diff_minor "
                    "FROM v_daily_position_arithmetic_breaks") == [(EUR_ACCT, "2026-01-04", 100)]
    assert rows(db, "SELECT diff_minor FROM v_position_vs_ledger_breaks") == [(100,)]


def test_anomaly_closing_inconsistent_with_ledger_but_row_adds_up(db):
    """Arithmetic is internally consistent, but no ledger entry backs the +100."""
    ins(db, "daily_cash_position", position("2026-01-04", 570000, 100, 0, 570100, 627110))
    assert rows(db, "SELECT * FROM v_daily_position_arithmetic_breaks") == []
    assert rows(db, "SELECT diff_minor FROM v_position_vs_ledger_breaks") == [(100,)]


def test_anomaly_opening_does_not_chain_from_previous_close(db):
    ins(db, "daily_cash_position", position("2026-01-04", 580000, 0, 0, 580000, 638000))
    assert rows(db, "SELECT diff_minor FROM v_daily_position_chain_breaks") == [(10000,)]


def test_anomaly_fx_rate_missing(db):
    ins(db, "daily_cash_position", position("2026-01-04", 570000, 0, 0, 570000, None,
                                            fx_rate_id=None))
    ins(db, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000020", fx_rate_id=None,
                                   base_amount_minor=None, external_ref="X20"))
    assert rows(db, "SELECT accounts_missing_fx FROM v_daily_entity_position "
                    "WHERE entity_id='ENT-000001' AND position_date='2026-01-04'") == [(1,)]
    assert rows(db, "SELECT record_id, issue FROM v_currency_integrity_issues") == [
        ("LED-2026-00000020", "FX_RATE_MISSING")]
    assert rows(db, "PRAGMA foreign_key_check") == []


def test_anomaly_cash_flow_counted_twice(db):
    # same bank movement posted twice and linked to the same receivable
    ins(db, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000003", amount_minor=100000,
                                   base_amount_minor=110000, entry_type="RECEIPT_SETTLEMENT",
                                   external_ref="R1"))
    ins(db, "settlement_link", link(receivable_id="RCV-2026-000001",
                                    ledger_entry_id="LED-2026-00000003",
                                    allocated_amount_minor=100000))
    assert rows(db, "SELECT settled_minor, outstanding_minor FROM v_receivable_settlement "
                    "WHERE receivable_id='RCV-2026-000001'") == [(200000, -100000)]
    dupes = db.execute(
        "SELECT COUNT(*) FROM ledger_entry WHERE external_ref='R1'").fetchone()[0]
    assert dupes == 2

    # the same source forecast twice in one run must also load
    ins(db, "cash_flow_forecast", cff(cash_flow_forecast_id="CF-2026-000020",
                                      source_type="PAYABLE", payable_id="PAY-2026-000001",
                                      direction="DEBIT"))
    ins(db, "cash_flow_forecast", cff(cash_flow_forecast_id="CF-2026-000021",
                                      source_type="PAYABLE", payable_id="PAY-2026-000001",
                                      direction="DEBIT"))
    assert db.execute("SELECT COUNT(*) FROM cash_flow_forecast WHERE payable_id='PAY-2026-000001' "
                      "AND forecast_run_id='FCAST-2026-01-03-001'").fetchone()[0] == 2


def test_anomaly_forecast_uses_actual_after_cutoff(db):
    ins(db, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000004", entry_date="2026-01-06",
                                   fx_rate_id=None, base_amount_minor=None, external_ref="X4",
                                   currency="EUR"))
    ins(db, "cash_flow_forecast", cff(
        cash_flow_forecast_id="CF-2026-000011", source_ledger_entry_id="LED-2026-00000004",
        source_data_date="2026-01-06", expected_date="2026-01-07", forecast_date="2026-01-07"))
    assert rows(db, "SELECT cash_flow_forecast_id, cutoff_date, source_data_date, "
                    "source_ledger_entry_date FROM v_forecast_cutoff_violations") == [
        ("CF-2026-000011", CUTOFF, "2026-01-06", "2026-01-06")]


def test_anomaly_currency_mismatch_and_unconverted_amount(db):
    ins(db, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000030", currency="JPY",
                                   external_ref="X30"))
    ins(db, "ledger_entry", ledger(ledger_entry_id="LED-2026-00000031", amount_minor=2000,
                                   base_amount_minor=2000, external_ref="X31"))
    got = sorted(rows(db, "SELECT record_id, issue FROM v_currency_integrity_issues"))
    assert got == [("LED-2026-00000030", "CURRENCY_MISMATCH"),
                   ("LED-2026-00000031", "UNCONVERTED_BASE_AMOUNT")]


def test_anomaly_rows_do_not_touch_unrelated_rows(db):
    """Injecting one break must flag exactly that row and nothing else."""
    ins(db, "daily_cash_position", position("2026-01-04", 570000, 0, 0, 570100, 627110))
    assert len(rows(db, "SELECT * FROM v_daily_position_arithmetic_breaks")) == 1
    assert rows(db, "SELECT * FROM v_forecast_cutoff_violations") == []
    assert rows(db, "SELECT * FROM v_currency_integrity_issues") == []


# ----------------------------------------------------------------------------
# 3. dictionary <-> schema agreement
# ----------------------------------------------------------------------------
def _schema_columns():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {t: [c[1] for c in conn.execute(f"PRAGMA table_info({t})")] for t in tables}


def _dictionary_sections():
    sections = {}
    current = None
    for line in DICTIONARY.splitlines():
        if line.startswith("## "):
            current = line
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _first_cell_names(lines):
    names = set()
    for line in lines:
        m = re.match(r"^\|\s*`([a-z][a-z0-9_]*)`", line)
        if m:
            names.add(m.group(1))
        names.update(re.findall(r"\(or `([a-z][a-z0-9_]*)`\)", line))
    return names


def _sections_for(table):
    return [(h, ls) for h, ls in _dictionary_sections().items()
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(table)}(?![A-Za-z0-9_])", h)]


@pytest.mark.parametrize("table", sorted(_schema_columns()))
def test_every_schema_column_is_documented(table):
    secs = _sections_for(table)
    assert secs, f"no dictionary section for table {table}"
    documented = set().union(*(_first_cell_names(ls) for _, ls in secs))
    missing = [c for c in _schema_columns()[table] if c not in documented]
    assert not missing, f"{table}: undocumented columns {missing}"


@pytest.mark.parametrize("table", sorted(_schema_columns()))
def test_dictionary_lists_no_phantom_columns(table):
    secs = _sections_for(table)
    documented = set().union(*(_first_cell_names(ls) for _, ls in secs))
    # a combined section may cover sibling tables; allow their columns too
    allowed = set()
    for h, _ in secs:
        for other, cols in _schema_columns().items():
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(other)}(?![A-Za-z0-9_])", h):
                allowed.update(cols)
    phantom = documented - allowed
    assert not phantom, f"{table}: dictionary lists columns not in schema {sorted(phantom)}"
