import sqlite3
from datetime import date

import pytest

from src import ids
from src.ids import IdFactory

from test_schema import SCHEMA, account, cff, comparison, entity, ins, ledger, receivable, run, scenario


# ----------------------------------------------------------------------------
# the five formats from the brief, exactly
# ----------------------------------------------------------------------------
def test_brief_examples_are_reproduced_exactly():
    assert ids.format_entity_id(1) == "ENT-000001"
    assert ids.format_bank_account_id("ENT-000001", 1) == "BANKACC-ENT-000001-001"
    assert ids.format_ledger_entry_id(2026, 1) == "LED-2026-00000001"
    assert ids.format_cash_flow_id(2026, 1) == "CF-2026-000001"
    assert ids.format_forecast_run_id("2026-01-01", 1) == "FCAST-2026-01-01-001"


def test_extension_formats():
    assert ids.format_receivable_id(2026, 7) == "RCV-2026-000007"
    assert ids.format_payable_id(2026, 7) == "PAY-2026-000007"
    assert ids.format_currency_rate_id("EUR", "2026-01-05") == "FXR-EUR-20260105"
    assert ids.format_scenario_id(3) == "SCN-003"
    assert ids.format_comparison_id(2026, 12) == "VAR-2026-000012"


# ----------------------------------------------------------------------------
# padding and bounds
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("fmt,width", [
    (ids.format_entity_id, 6),
    (ids.format_scenario_id, 3),
])
def test_counter_bounds_single_arg(fmt, width):
    top = 10**width - 1
    assert fmt(top).endswith(str(top))
    for bad in (0, -1, top + 1):
        with pytest.raises(ValueError):
            fmt(bad)


@pytest.mark.parametrize("fmt,width", [
    (ids.format_ledger_entry_id, 8),
    (ids.format_cash_flow_id, 6),
    (ids.format_receivable_id, 6),
    (ids.format_payable_id, 6),
    (ids.format_comparison_id, 6),
])
def test_counter_bounds_year_formats(fmt, width):
    top = 10**width - 1
    assert len(fmt(2026, top)) == len(fmt(2026, 1))  # same length at both extremes
    for bad in (0, -1, top + 1):
        with pytest.raises(ValueError):
            fmt(2026, bad)


def test_account_and_run_counters_stop_at_999():
    assert ids.format_bank_account_id("ENT-000001", 999).endswith("-999")
    assert ids.format_forecast_run_id("2026-01-01", 999).endswith("-999")
    with pytest.raises(ValueError):
        ids.format_bank_account_id("ENT-000001", 1000)
    with pytest.raises(ValueError):
        ids.format_forecast_run_id("2026-01-01", 1000)


@pytest.mark.parametrize("bad", [True, 1.0, "1", None])
def test_counter_must_be_a_real_int(bad):
    with pytest.raises(ValueError):
        ids.format_entity_id(bad)


@pytest.mark.parametrize("bad_year", [26, 20260, 0, -2026, "2026", None, True])
def test_year_must_be_four_digit_int(bad_year):
    with pytest.raises(ValueError):
        ids.format_ledger_entry_id(bad_year, 1)


def test_bank_account_requires_a_valid_entity_id():
    for bad in ("ENT-1", "ent-000001", "ENT-0000011", "BANKACC-ENT-000001-001", "", None):
        with pytest.raises(ValueError):
            ids.format_bank_account_id(bad, 1)


@pytest.mark.parametrize("bad", ["2026-1-1", "20260101", "2026-02-30", "2026-13-01",
                                 "2026-01-01 ", "01-01-2026", "", None, 20260101])
def test_run_date_must_be_a_strict_real_iso_date(bad):
    with pytest.raises(ValueError):
        ids.format_forecast_run_id(bad, 1)


def test_run_id_accepts_date_objects_and_iso_strings_alike():
    assert (ids.format_forecast_run_id(date(2026, 3, 9), 2)
            == ids.format_forecast_run_id("2026-03-09", 2)
            == "FCAST-2026-03-09-002")


def test_currency_rate_id_rejects_bad_currency():
    for bad in ("eur", "EU", "EURO", "E1R", "", None):
        with pytest.raises(ValueError):
            ids.format_currency_rate_id(bad, "2026-01-05")


# ----------------------------------------------------------------------------
# zero padding exists so that string order equals numeric order
# ----------------------------------------------------------------------------
def test_string_order_equals_numeric_order():
    numbers = [1, 2, 9, 10, 11, 99, 100, 999, 1000, 5000, 99999, 100000]
    assert [ids.format_ledger_entry_id(2026, n) for n in numbers] == sorted(
        ids.format_ledger_entry_id(2026, n) for n in numbers)
    assert [ids.format_entity_id(n) for n in numbers] == sorted(ids.format_entity_id(n) for n in numbers)


# ----------------------------------------------------------------------------
# parsing and classification
# ----------------------------------------------------------------------------
SAMPLES = {
    "entity": "ENT-000001",
    "bank_account": "BANKACC-ENT-000001-001",
    "ledger_entry": "LED-2026-00000001",
    "cash_flow": "CF-2026-000001",
    "forecast_run": "FCAST-2026-01-01-001",
    "receivable": "RCV-2026-000001",
    "payable": "PAY-2026-000001",
    "currency_rate": "FXR-EUR-20260105",
    "scenario": "SCN-001",
    "comparison": "VAR-2026-000001",
}


def test_every_kind_has_a_sample_and_classifies_correctly():
    assert set(SAMPLES) == set(ids.ID_PATTERNS)
    for kind, sample in SAMPLES.items():
        assert ids.id_kind(sample) == kind


@pytest.mark.parametrize("bad", [
    "ENT-000001\n", " ENT-000001", "ent-000001", "ENT-00001", "ENT-0000001",
    "LED-2026-0000001", "LED-26-00000001", "CF-2026-00000a",
    "FCAST-2026-1-01-001", "FCAST-2026-01-01-01",
    "BANKACC-000001-001", "BANKACC-ENT-000001-1", "",
])
def test_malformed_ids_do_not_classify(bad):
    assert ids.id_kind(bad) is None


def test_non_ascii_digits_are_rejected():
    assert ids.id_kind("ENT-١٢٣٤٥٦") is None  # Arabic-Indic digits


def test_split_bank_account_id_round_trip():
    acct = ids.format_bank_account_id("ENT-000042", 7)
    assert ids.split_bank_account_id(acct) == ("ENT-000042", 7)
    with pytest.raises(ValueError):
        ids.split_bank_account_id("BANKACC-000042-007")


def test_year_and_sequence_extraction():
    assert ids.year_of("LED-2027-00000005") == 2027
    assert ids.sequence_of("LED-2027-00000005") == 5
    assert ids.sequence_of("FCAST-2026-01-01-012") == 12
    with pytest.raises(ValueError):
        ids.year_of("ENT-000001")
    with pytest.raises(ValueError):
        ids.sequence_of("FXR-EUR-20260105")


# ----------------------------------------------------------------------------
# factory: counters, independence, immutability
# ----------------------------------------------------------------------------
def test_factory_counts_from_one_and_never_repeats():
    f = IdFactory()
    issued = [f.entity() for _ in range(50)]
    assert issued[0] == "ENT-000001" and issued[-1] == "ENT-000050"
    assert len(set(issued)) == 50


def test_account_counters_are_per_entity():
    f = IdFactory()
    e1, e2 = f.entity(), f.entity()
    assert [f.bank_account(e1), f.bank_account(e1), f.bank_account(e2)] == [
        "BANKACC-ENT-000001-001", "BANKACC-ENT-000001-002", "BANKACC-ENT-000002-001"]


def test_yearly_counters_restart_each_year_and_run_counters_each_date():
    f = IdFactory()
    assert f.ledger_entry(2026) == "LED-2026-00000001"
    assert f.ledger_entry(2026) == "LED-2026-00000002"
    assert f.ledger_entry(2027) == "LED-2027-00000001"
    assert f.ledger_entry(2026) == "LED-2026-00000003"
    assert f.forecast_run("2026-01-01") == "FCAST-2026-01-01-001"
    assert f.forecast_run(date(2026, 1, 1)) == "FCAST-2026-01-01-002"  # same date, either type
    assert f.forecast_run("2026-01-02") == "FCAST-2026-01-02-001"


def test_different_kinds_do_not_share_counters():
    f = IdFactory()
    assert f.ledger_entry(2026).endswith("00000001")
    assert f.cash_flow(2026).endswith("000001")
    assert f.receivable(2026).endswith("000001")
    assert f.payable(2026).endswith("000001")
    assert f.comparison(2026).endswith("000001")
    assert f.scenario() == "SCN-001"


def test_rejected_request_does_not_consume_a_counter():
    f = IdFactory()
    with pytest.raises(ValueError):
        f.ledger_entry(99)            # bad year
    assert f.issued("ledger_entry", 99) == 0
    with pytest.raises(ValueError):
        f.bank_account("not-an-entity")
    assert f.ledger_entry(2026) == "LED-2026-00000001"


def test_counter_overflow_raises_and_leaves_state_intact():
    f = IdFactory()
    e = f.entity()
    for _ in range(999):
        f.bank_account(e)
    with pytest.raises(ValueError):
        f.bank_account(e)
    assert f.issued("bank_account", e) == 999


def test_two_factories_issue_identical_sequences():
    def run_once():
        f = IdFactory()
        e = f.entity()
        return [e, f.bank_account(e), f.ledger_entry(2026), f.cash_flow(2026),
                f.forecast_run("2026-02-01"), f.receivable(2026), f.payable(2026),
                f.scenario(), f.comparison(2026)]

    assert run_once() == run_once()


def test_everything_the_factory_issues_classifies_as_its_own_kind():
    f = IdFactory()
    e = f.entity()
    expected = {
        "entity": e, "bank_account": f.bank_account(e), "ledger_entry": f.ledger_entry(2026),
        "cash_flow": f.cash_flow(2026), "forecast_run": f.forecast_run("2026-01-01"),
        "receivable": f.receivable(2026), "payable": f.payable(2026),
        "scenario": f.scenario(), "comparison": f.comparison(2026),
    }
    for kind, value in expected.items():
        assert ids.id_kind(value) == kind


# ----------------------------------------------------------------------------
# helper output must satisfy the CHECK constraints in sql/schema.sql
# ----------------------------------------------------------------------------
def test_helper_ids_are_accepted_by_the_schema():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")  # this test is about ID format only

    f = IdFactory()
    e = f.entity()
    acct = f.bank_account(e)

    ins(conn, "legal_entity", entity(entity_id=e))
    ins(conn, "bank_account", account(bank_account_id=acct, entity_id=e, account_seq=1))
    ins(conn, "ledger_entry", ledger(ledger_entry_id=f.ledger_entry(2026), bank_account_id=acct))
    ins(conn, "receivable", receivable(receivable_id=f.receivable(2026), entity_id=e))
    ins(conn, "payable", dict(
        payable_id=f.payable(2026), entity_id=e, bank_account_id=acct, payee_name="P",
        payable_category="TRADE", is_one_off=0, document_ref="B1", currency="EUR",
        amount_minor=1, invoice_date="2026-01-01", due_date="2026-01-02",
        expected_date="2026-01-02", status="OPEN"))
    ins(conn, "currency_rate", dict(
        rate_id=ids.format_currency_rate_id("EUR", "2026-01-05"), rate_date="2026-01-05",
        quote_currency="EUR", base_currency="USD", rate=1.1, rate_source="SYNTHETIC_RANDOM_WALK"))
    ins(conn, "liquidity_scenario", scenario(scenario_id=f.scenario()))
    ins(conn, "forecast_run", run(forecast_run_id=f.forecast_run("2026-01-04"), run_seq=1))
    ins(conn, "cash_flow_forecast", cff(cash_flow_forecast_id=f.cash_flow(2026)))
    ins(conn, "forecast_vs_actual", comparison(comparison_id=f.comparison(2026)))


def test_schema_rejects_what_the_helpers_would_never_produce():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError):
        ins(conn, "legal_entity", entity(entity_id="ENT-1"))
    with pytest.raises(sqlite3.IntegrityError):
        ins(conn, "bank_account", account(bank_account_id="BANKACC-000002-002"))
