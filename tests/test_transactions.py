"""Transactions: receivables, payables, ledger, status history, derived positions, reconciliation."""
import hashlib
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import build_config
from src.db import create_database, load_tables
from src.generate import main
from src.pipeline import generate_all
from src.txn import params as P
from src.txn.common import Calendar, RateBook
from src.txn.derive import consolidate, daily_cash_positions, running_balances
from src.txn.events import ALLOWED
from src.validate.reconcile import reconcile

ROOT = Path(__file__).resolve().parent.parent


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
def db(built):
    _, t = built
    conn = create_database()
    load_tables(conn, t)
    return conn


# ---- sizes and keys -----------------------------------------------------------
def test_ledger_has_exactly_the_configured_rows(built):
    cfg, t = built
    assert len(t["ledger_entry"]) == cfg.n_ledger


def test_ledger_composition_matches_the_documented_shares(full):
    cfg, t = full
    counts = t["ledger_entry"].entry_type.value_counts()
    assert counts["FEE_OR_INTEREST"] == round(P.FEE_SHARE * cfg.n_ledger)
    assert counts["REVERSAL"] == round(P.REVERSAL_PAIR_SHARE * cfg.n_ledger)
    settle = counts["RECEIPT_SETTLEMENT"] + counts["PAYMENT_SETTLEMENT"]
    assert abs(settle / cfg.n_ledger - P.SETTLEMENT_SHARE) <= P.SETTLEMENT_SHARE * P.SETTLEMENT_TOLERANCE
    assert counts["OPERATING_INFLOW"] > 0 and counts["OPERATING_OUTFLOW"] > 0


def test_primary_keys_unique(built):
    _, t = built
    for table, col in [("receivable", "receivable_id"), ("payable", "payable_id"),
                       ("ledger_entry", "ledger_entry_id"), ("settlement_link", "settlement_link_id"),
                       ("receivable_status_event", "event_id"), ("payable_status_event", "event_id")]:
        assert t[table][col].is_unique, table
    assert t["ledger_entry"].external_ref.is_unique
    for table in ("receivable", "payable"):
        assert not t[table].duplicated(["entity_id", "document_ref"]).any()


def test_everything_loads_into_sqlite_with_foreign_keys_checked(db):
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_ledger_ids_follow_posting_order_with_per_year_counters(built):
    _, t = built
    led = t["ledger_entry"]
    for year, g in led.groupby(led.entry_date.str[:4]):
        n = g.ledger_entry_id.str[-8:].astype(int).to_numpy()
        assert n.tolist() == list(range(1, len(g) + 1)), year
    ordered = led.sort_values(["entry_date", "bank_account_id"], kind="stable")
    assert ordered.index.is_monotonic_increasing


# ---- parent IDs ------------------------------------------------------------------
def test_every_parent_reference_resolves(built):
    _, t = built
    acct = set(t["bank_account"].bank_account_id)
    led = set(t["ledger_entry"].ledger_entry_id)
    assert set(t["ledger_entry"].bank_account_id) <= acct
    assert set(t["receivable"].bank_account_id) <= acct and set(t["payable"].bank_account_id) <= acct
    links = t["settlement_link"]
    assert set(links.ledger_entry_id) <= led
    assert set(links.receivable_id.dropna()) <= set(t["receivable"].receivable_id)
    assert set(links.payable_id.dropna()) <= set(t["payable"].payable_id)
    for ev, col, parent in (("receivable_status_event", "receivable_id", "receivable"),
                            ("payable_status_event", "payable_id", "payable")):
        assert set(t[ev][col]) == set(t[parent][col])           # every document has history
        assert set(t[ev].ledger_entry_id.dropna()) <= led


def test_item_currency_and_entity_match_the_account(built):
    _, t = built
    acc = t["bank_account"].set_index("bank_account_id")
    for table in ("receivable", "payable"):
        df = t[table]
        assert (df.currency == df.bank_account_id.map(acc.currency)).all()
        assert (df.entity_id == df.bank_account_id.map(acc.entity_id)).all()
    led = t["ledger_entry"]
    assert (led.currency == led.bank_account_id.map(acc.currency)).all()


def test_settlement_entries_have_links_and_other_entries_have_none(built):
    _, t = built
    led, links = t["ledger_entry"], t["settlement_link"]
    linked = set(links.ledger_entry_id)
    is_settlement = led.entry_type.isin(["RECEIPT_SETTLEMENT", "PAYMENT_SETTLEMENT"])
    assert set(led.ledger_entry_id[is_settlement]) == linked
    # a receipt links only receivables and a payment only payables
    kind = links.merge(led[["ledger_entry_id", "entry_type"]], on="ledger_entry_id")
    assert (kind.receivable_id.notna() == (kind.entry_type == "RECEIPT_SETTLEMENT")).all()


def test_link_allocations_add_up_to_the_ledger_amount(built):
    _, t = built
    sums = t["settlement_link"].groupby("ledger_entry_id").allocated_amount_minor.sum()
    led = t["ledger_entry"].set_index("ledger_entry_id")
    assert (sums == led.loc[sums.index, "amount_minor"]).all()


def test_many_documents_to_one_entry_and_partial_payments_both_exist(full):
    _, t = full
    links = t["settlement_link"]
    per_entry = links.groupby("ledger_entry_id").size()
    assert (per_entry > 1).sum() > 100 and per_entry.max() <= P.BATCH_MAX_ITEMS
    doc = links.receivable_id.fillna(links.payable_id)
    assert (doc.value_counts() > 1).sum() > 50                # documents paid in several entries


def test_batched_documents_share_account_and_counterparty(full):
    _, t = full
    links = t["settlement_link"].merge(t["receivable"], on="receivable_id")
    led = t["ledger_entry"].set_index("ledger_entry_id")
    g = links.groupby("ledger_entry_id")
    assert (g.counterparty_name.nunique() == 1).all()
    assert (links.bank_account_id == links.ledger_entry_id.map(led.bank_account_id)).all()


# ---- status lifecycle ----------------------------------------------------------------
@pytest.mark.parametrize("kind,table,col", [("R", "receivable", "receivable_id"), ("P", "payable", "payable_id")])
def test_every_transition_is_allowed_and_status_is_the_last_event(built, kind, table, col):
    _, t = built
    ev = t[f"{table}_status_event"].sort_values([col, "event_seq"])
    first = ev.groupby(col).head(1)
    assert (first.event_seq == 1).all() and (first.cause == "CREATED").all()
    assert ev.groupby(col).event_seq.apply(lambda s: s.tolist() == list(range(1, len(s) + 1))).all()
    nxt = ev[ev.event_seq > 1]
    for frm, to in zip(nxt.from_status, nxt.to_status):
        assert to in ALLOWED[kind][frm], (frm, to)
    prev_to = ev.groupby(col).to_status.shift(1)
    assert (ev.from_status.fillna("x") == prev_to.fillna("x")).all()           # chain of statuses
    last = ev.groupby(col).tail(1).set_index(col).to_status
    assert (t[table].set_index(col).status == last.reindex(t[table][col]).to_numpy()).all()
    d = pd.to_datetime(ev.event_date)
    assert (d.groupby(ev[col]).diff().dropna() >= pd.Timedelta(0)).all()      # dates never go back


def test_all_documented_statuses_occur_at_full_scale(full):
    _, t = full
    assert set(t["receivable"].status) == {"OPEN", "PARTIALLY_SETTLED", "OVERDUE", "SETTLED",
                                           "WRITTEN_OFF", "CANCELLED"}
    assert set(t["payable"].status) == {"OPEN", "PARTIALLY_SETTLED", "OVERDUE", "SETTLED", "CANCELLED"}


def test_stored_status_agrees_with_what_the_ledger_settled(built):
    _, t = built
    for table, col, link_col in (("receivable", "receivable_id", "receivable_id"),
                                 ("payable", "payable_id", "payable_id")):
        paid = t["settlement_link"].groupby(link_col).allocated_amount_minor.sum()
        df = t[table].set_index(col)
        got = paid.reindex(df.index).fillna(0)
        assert (got <= df.amount_minor).all()
        assert (got[df.status == "SETTLED"] == df.amount_minor[df.status == "SETTLED"]).all()
        part = df.status == "PARTIALLY_SETTLED"
        assert ((got[part] > 0) & (got[part] < df.amount_minor[part])).all()
        assert (got[df.status.isin(["OPEN", "CANCELLED"])] == 0).all()


def test_overdue_rows_still_owe_money_and_were_not_paid_in_full(built):
    _, t = built
    paid = t["settlement_link"].groupby("receivable_id").allocated_amount_minor.sum()
    ov = t["receivable"].set_index("receivable_id")
    ov = ov[ov.status == "OVERDUE"]
    assert (paid.reindex(ov.index).fillna(0) < ov.amount_minor).all()


# ---- delayed receivables and one-off payments --------------------------------------
def test_delayed_receivables_settle_after_the_expected_date(full):
    _, t = full
    r = t["receivable"].set_index("receivable_id")
    last = (t["settlement_link"].dropna(subset=["receivable_id"])
            .merge(t["ledger_entry"][["ledger_entry_id", "entry_date"]], on="ledger_entry_id")
            .groupby("receivable_id").entry_date.max())
    settled = r[r.status == "SETTLED"].join(last.rename("settled_on"))
    late = settled.settled_on > settled.expected_date
    assert 0.10 < late.mean() < 0.40
    assert (settled.settled_on[late] > settled.expected_date[late]).all()
    days = (pd.to_datetime(settled.settled_on) - pd.to_datetime(settled.expected_date)).dt.days
    assert days.max() <= P.LATE_MAX_DAYS + 2 * P.PARTIAL_GAP_MEAN_DAYS * 10      # bounded, not unbounded


def test_late_rate_rises_with_customer_risk_tier(full):
    _, t = full
    r = t["receivable"].set_index("receivable_id")
    first = (t["settlement_link"].dropna(subset=["receivable_id"])
             .merge(t["ledger_entry"][["ledger_entry_id", "entry_date"]], on="ledger_entry_id")
             .groupby("receivable_id").entry_date.min().rename("first_paid"))
    s = r[r.status.isin(["SETTLED", "PARTIALLY_SETTLED"])].join(first)
    s = s[s.first_paid.notna()]
    s["late"] = s.first_paid > s.expected_date
    rate = s.groupby("counterparty_risk_tier").late.mean()
    assert rate["LOW"] < rate["MEDIUM"] < rate["HIGH"]
    assert rate["HIGH"] > 0.4 and rate["LOW"] < 0.3


def test_large_one_off_payments(full, starter):
    for cfg, t in (full, starter):
        pay = t["payable"]
        one = pay[pay.is_one_off == 1]
        assert len(one) <= max(P.ONE_OFF_MIN_COUNT, round(P.ONE_OFF_PER_ENTITY * cfg.n_entities))
        assert len(one) >= P.ONE_OFF_MIN_COUNT - 1 or cfg.n_entities < 6
        assert set(one.payable_category) <= {"CAPEX", "OTHER"}
    pay = full[1]["payable"]
    one = pay[pay.is_one_off == 1]
    rb = RateBook(full[0], full[1]["currency_rate"], Calendar(full[0]))
    cal = Calendar(full[0])
    usd = [float(rb.to_base_minor(a, c, cal.offset(d))) / 100
           for a, c, d in zip(one.amount_minor, one.currency, one.invoice_date)]
    trade = pay[pay.payable_category == "TRADE"]
    usd_trade = [float(rb.to_base_minor(a, c, max(cal.offset(d), 0))) / 100
                 for a, c, d in zip(trade.amount_minor[:3000], trade.currency[:3000], trade.invoice_date[:3000])]
    assert np.median(usd) > 10 * np.median(usd_trade)


def test_scheduled_payables_exist_in_every_category(full):
    _, t = full
    assert set(t["payable"].payable_category) == {"TRADE", "PAYROLL", "TAX", "DEBT_SERVICE", "CAPEX", "OTHER"}
    pay = t["payable"]
    payroll = pay[pay.payable_category == "PAYROLL"]
    assert (pd.to_datetime(payroll.invoice_date).dt.day <= P.PAYROLL_PAY_DAY).all()
    assert (pd.to_datetime(payroll.invoice_date).dt.weekday < 5).all()
    tax = pay[pay.payable_category == "TAX"]
    assert set(pd.to_datetime(tax.due_date).dt.month) == set(P.TAX_MONTHS)


def test_payroll_runs_monthly_per_account(full):
    _, t = full
    pay = t["payable"]
    payroll = pay[pay.payable_category == "PAYROLL"]
    per_acct = payroll.groupby("bank_account_id").size()
    assert per_acct.max() == 12


# ---- dates and lifecycle limits of accounts ---------------------------------------------
def test_ledger_dates_are_business_days_inside_each_accounts_life(built):
    _, t = built
    led = t["ledger_entry"].merge(t["bank_account"], on="bank_account_id")
    d = pd.to_datetime(led.entry_date)
    assert (d.dt.weekday < 5).all()
    assert (led.entry_date >= led.opening_date).all()
    ended = led.effective_to.notna()
    assert (led.entry_date[ended] <= led.effective_to[ended]).all()


def test_dormant_accounts_carry_less_activity_per_live_day(full):
    _, t = full
    acc = t["bank_account"]
    n = t["ledger_entry"].groupby("bank_account_id").size().reindex(acc.bank_account_id).fillna(0).to_numpy()
    live_days = [(min(pd.Timestamp(e) if isinstance(e, str) else pd.Timestamp("2026-12-31"), pd.Timestamp("2026-12-31"))
                  - pd.Timestamp(o)).days + 1 for o, e in zip(acc.opening_date, acc.effective_to)]
    rate = pd.Series(n / np.array(live_days), index=acc.bank_account_id.to_numpy())
    dormant = acc.status.eq("DORMANT").to_numpy()
    active = acc.status.eq("ACTIVE").to_numpy()
    assert rate[dormant].mean() < 0.5 * rate[active].mean()


def test_pre_window_invoices_exist_and_have_events_on_their_own_date(full):
    _, t = full
    pre = t["receivable"][t["receivable"].invoice_date < "2026-01-01"]
    assert len(pre) > 50
    ev = t["receivable_status_event"].merge(t["receivable"], on="receivable_id")
    created = ev[ev.cause == "CREATED"]
    assert (created.event_date == created.invoice_date).all()


# ---- amounts, currencies, FX ---------------------------------------------------------------
def test_amounts_positive_and_directions_follow_entry_type(built):
    _, t = built
    led = t["ledger_entry"]
    assert (led.amount_minor > 0).all() and (led.base_amount_minor > 0).all()
    credit = {"RECEIPT_SETTLEMENT", "OPERATING_INFLOW"}
    debit = {"PAYMENT_SETTLEMENT", "OPERATING_OUTFLOW"}
    assert (led.direction[led.entry_type.isin(credit)] == "CREDIT").all()
    assert (led.direction[led.entry_type.isin(debit)] == "DEBIT").all()


def test_base_amount_uses_that_days_rate_with_exponent_adjustment(built):
    cfg, t = built
    led = t["ledger_entry"]
    rates = t["currency_rate"].set_index("rate_id").rate
    exp = {c.code: c.minor_unit_exponent for c in cfg.currencies}
    non_base = led[led.currency != "USD"]
    r = non_base.fx_rate_id.map(rates)
    expected = np.rint(non_base.amount_minor * r * 10.0 ** (2 - non_base.currency.map(exp)))
    assert (non_base.base_amount_minor == expected).all()
    # the rate used is the one for the entry date and currency
    assert (non_base.fx_rate_id.str[4:7] == non_base.currency).all()
    assert (non_base.fx_rate_id.str[8:] == non_base.entry_date.str.replace("-", "")).all()
    usd = led[led.currency == "USD"]
    assert usd.fx_rate_id.isna().all() and (usd.base_amount_minor == usd.amount_minor).all()


def test_every_account_currency_has_ledger_activity(full):
    _, t = full
    assert set(t["ledger_entry"].currency) == {"USD", "EUR", "GBP", "INR", "JPY"}


# ---- reversals ---------------------------------------------------------------------------------
def test_reversals_mirror_their_original(built):
    _, t = built
    led = t["ledger_entry"].set_index("ledger_entry_id")
    rev = led[led.entry_type == "REVERSAL"]
    assert len(rev) > 0
    orig = led.loc[rev.reverses_ledger_entry_id]
    assert (orig.entry_type.isin(["OPERATING_INFLOW", "OPERATING_OUTFLOW"])).all()
    assert (orig.bank_account_id.to_numpy() == rev.bank_account_id.to_numpy()).all()
    assert (orig.amount_minor.to_numpy() == rev.amount_minor.to_numpy()).all()
    assert (orig.direction.to_numpy() != rev.direction.to_numpy()).all()
    gap = (pd.to_datetime(rev.entry_date).to_numpy() - pd.to_datetime(orig.entry_date).to_numpy()).astype("timedelta64[D]").astype(int)
    assert ((gap >= 1) & (gap <= P.REVERSAL_MAX_GAP_DAYS + 2)).all()
    assert rev.reverses_ledger_entry_id.is_unique


# ---- derived positions and the reconciliation check ------------------------------------------
def test_one_position_row_per_account_and_live_calendar_day(built):
    cfg, t = built
    pos, acc = t["daily_cash_position"], t["bank_account"]
    assert not pos.duplicated(["bank_account_id", "position_date"]).any()
    for r in acc.itertuples(index=False):
        end = min(pd.Timestamp(r.effective_to), pd.Timestamp(cfg.date_end)) if isinstance(r.effective_to, str) \
            else pd.Timestamp(cfg.date_end)
        want = pd.date_range(r.opening_date, end)
        got = pos[pos.bank_account_id == r.bank_account_id].position_date
        assert len(got) == len(want) and got.iloc[0] == str(want[0].date()) and got.iloc[-1] == str(want[-1].date())


def test_reconciliation_is_clean(built):
    _, t = built
    assert len(reconcile(t["ledger_entry"], t["daily_cash_position"], t["bank_account"])) == 0


def test_sql_views_agree_that_there_are_no_breaks(db):
    for v in ("v_daily_position_arithmetic_breaks", "v_daily_position_chain_breaks",
              "v_position_vs_ledger_breaks", "v_currency_integrity_issues"):
        assert db.execute(f"SELECT COUNT(*) FROM {v}").fetchone()[0] == 0, v


def test_closing_balance_equals_opening_plus_ledger_for_every_account_day(built):
    _, t = built
    pos = t["daily_cash_position"]
    led = t["ledger_entry"]
    sign = np.where(led.direction == "CREDIT", 1, -1) * led.amount_minor
    net = pd.Series(sign.to_numpy(), index=pd.MultiIndex.from_frame(led[["bank_account_id", "entry_date"]]))
    net = net.groupby(level=[0, 1]).sum()
    opening = t["bank_account"].set_index("bank_account_id").opening_balance_minor
    pos = pos.sort_values(["bank_account_id", "position_date"])
    idx = pd.MultiIndex.from_frame(pos[["bank_account_id", "position_date"]])
    daily = net.reindex(idx).fillna(0).to_numpy()
    cum = pd.Series(daily, index=pos.index).groupby(pos.bank_account_id).cumsum()
    assert (pos.closing_minor == cum + pos.bank_account_id.map(opening)).all()


def test_running_balance_after_last_entry_equals_that_days_closing(built):
    _, t = built
    rb = running_balances(t["ledger_entry"], t["bank_account"])
    last = rb.groupby(["bank_account_id", "entry_date"]).balance_after_minor.last().rename("b").reset_index()
    pos = t["daily_cash_position"].merge(last, left_on=["bank_account_id", "position_date"],
                                         right_on=["bank_account_id", "entry_date"])
    assert len(pos) > 0 and (pos.closing_minor == pos.b).all()


def test_quiet_days_carry_the_balance_forward(built):
    _, t = built
    pos = t["daily_cash_position"]
    quiet = pos[pos.entry_count == 0]
    assert len(quiet) > 0
    assert (quiet.opening_minor == quiet.closing_minor).all()
    assert (quiet.total_credits_minor == 0).all() and (quiet.total_debits_minor == 0).all()


def test_closing_base_uses_the_rate_of_that_day(built):
    cfg, t = built
    pos = t["daily_cash_position"]
    rates = t["currency_rate"].set_index("rate_id").rate
    exp = {c.code: c.minor_unit_exponent for c in cfg.currencies}
    nb = pos[pos.currency != "USD"]
    expected = np.rint(nb.closing_minor * nb.fx_rate_id.map(rates) * 10.0 ** (2 - nb.currency.map(exp)))
    assert (nb.closing_base_minor == expected).all()
    assert (nb.fx_rate_id.str[8:] == nb.position_date.str.replace("-", "")).all()
    usd = pos[pos.currency == "USD"]
    assert (usd.closing_base_minor == usd.closing_minor).all() and usd.fx_rate_id.isna().all()


def test_weekend_positions_use_fridays_rate(built):
    _, t = built
    pos = t["daily_cash_position"].merge(t["currency_rate"][["rate_id", "rate"]], left_on="fx_rate_id", right_on="rate_id")
    sat = pos[pd.to_datetime(pos.position_date).dt.weekday == 5]
    fri = pos.assign(d=pd.to_datetime(pos.position_date) + pd.Timedelta(days=1))
    both = sat.assign(d=pd.to_datetime(sat.position_date)).merge(
        fri[["bank_account_id", "d", "rate"]], on=["bank_account_id", "d"], suffixes=("", "_fri"))
    assert len(both) > 0 and (both.rate == both.rate_fri).all()


def test_consolidation_equals_the_sql_view_and_sums_accounts(db, built):
    _, t = built
    cons = consolidate(t["daily_cash_position"], t["bank_account"])
    group = cons[cons.entity_id == "GROUP"].set_index("position_date").closing_base_minor
    by_day = t["daily_cash_position"].groupby("position_date").closing_base_minor.sum()
    assert (group == by_day.reindex(group.index)).all()
    ent = cons[cons.entity_id != "GROUP"].groupby("position_date").closing_base_minor.sum()
    assert (ent == group.reindex(ent.index)).all()
    view = pd.read_sql("SELECT * FROM v_daily_entity_position", db)
    m = view.merge(cons, on=["entity_id", "position_date"], suffixes=("_sql", "_py"))
    assert len(m) == len(view) == len(cons[cons.entity_id != "GROUP"])
    assert (m.closing_base_minor_sql == m.closing_base_minor_py).all()
    assert (m.account_count == m.n_accounts).all()


@pytest.mark.parametrize("which", ["opening", "credits", "closing", "chain", "drop_ledger_row", "extra_ledger_row"])
def test_reconciliation_detects_injected_breaks(starter, which):
    _, t = starter
    pos = t["daily_cash_position"].copy()
    led = t["ledger_entry"].copy()
    acc = t["bank_account"]
    i = 1000
    if which == "opening":
        pos.loc[i, "opening_minor"] += 7
    elif which == "credits":
        pos.loc[i, "total_credits_minor"] += 5
    elif which == "closing":
        pos.loc[i, "closing_minor"] += 3
    elif which == "chain":
        pos.loc[i:, "opening_minor"] += 1          # a bad opening on a mid-account row
        pos = pos.loc[[i]]
        pos.loc[i, "closing_minor"] += 1
    elif which == "drop_ledger_row":
        led = led.drop(led.index[500])
    else:
        extra = led.iloc[[10]].copy()
        extra["ledger_entry_id"] = "LED-2026-99999999"
        led = pd.concat([led, extra])
    breaks = reconcile(led, pos, acc)
    assert len(breaks) > 0


def test_each_injected_break_is_found_by_the_check_that_should_find_it(starter):
    _, t = starter
    acc = t["bank_account"]
    pos = t["daily_cash_position"].copy()
    pos.loc[1000, "total_credits_minor"] += 5
    assert set(reconcile(t["ledger_entry"], pos, acc).check) >= {"ARITHMETIC", "CREDITS_VS_LEDGER"}
    pos = t["daily_cash_position"].copy()
    pos.loc[1000, "opening_minor"] += 7
    assert "CHAIN" in set(reconcile(t["ledger_entry"], pos, acc).check)
    led = t["ledger_entry"].copy()
    bad = led.drop(led.index[500])
    assert {"LEDGER_CUMULATIVE", "COUNT_VS_LEDGER"} <= set(reconcile(bad, t["daily_cash_position"], acc).check)


def test_reconciliation_break_is_also_caught_by_the_sql_views(starter):
    _, t = starter
    tables = {k: v.copy() for k, v in t.items()}
    tables["daily_cash_position"].loc[1000, "closing_minor"] += 3
    conn = create_database()
    load_tables(conn, tables)
    assert conn.execute("SELECT COUNT(*) FROM v_daily_position_arithmetic_breaks").fetchone()[0] > 0
    assert len(reconcile(tables["ledger_entry"], tables["daily_cash_position"], tables["bank_account"])) > 0


def test_generation_refuses_to_continue_when_reconciliation_fails(monkeypatch):
    import src.txn as txn
    real = txn.daily_cash_positions

    def broken(*a, **k):
        df = real(*a, **k)
        df.loc[5, "closing_minor"] += 1
        return df
    monkeypatch.setattr(txn, "daily_cash_positions", broken)
    with pytest.raises(txn.ReconciliationError):
        generate_all(build_config("starter"))


def test_no_settlement_is_posted_before_its_document_exists(built):
    _, t = built
    led = t["ledger_entry"].set_index("ledger_entry_id").entry_date
    for table, col in (("receivable", "receivable_id"), ("payable", "payable_id")):
        m = t["settlement_link"].dropna(subset=[col]).merge(t[table], on=col)
        m["posted"] = m.ledger_entry_id.map(led)
        assert (m.posted >= m.invoice_date).all(), table
        # scheduled payables (payroll, debt service) are invoiced and paid the same day by design
        ordinary = m if table == "receivable" else m[m.payable_category == "TRADE"]
        assert (ordinary.posted > ordinary.invoice_date).all(), table


# ---- balance behaviour (the feedback loop) ------------------------------------------------------
def test_flows_that_pull_a_balance_back_are_larger_than_flows_that_push_it_away(full):
    _, t = full
    rb = running_balances(t["ledger_entry"], t["bank_account"])
    reg = rb[rb.entry_type.isin(["OPERATING_INFLOW", "OPERATING_OUTFLOW"])].copy()
    reg["before"] = reg.balance_after_minor - reg.signed_minor
    ref = reg.groupby("bank_account_id").before.transform("median")
    toward = np.sign(ref - reg.before) == np.sign(reg.signed_minor)
    rel = reg.amount_minor / reg.groupby("bank_account_id").amount_minor.transform("median")
    assert rel[toward].mean() > 1.15 * rel[~toward].mean()


def test_balances_stay_in_a_plausible_range(full):
    _, t = full
    pos = t["daily_cash_position"]
    assert (pos.closing_minor < 0).mean() < 0.15            # shortfalls exist but are the exception
    assert (pos.closing_minor < 0).mean() > 0.0
    base = pos.closing_base_minor / 100
    assert base.abs().max() < 5e8


# ---- determinism ------------------------------------------------------------------------------------
def digest(tables):
    h = hashlib.sha256()
    for k in sorted(tables):
        h.update(k.encode())
        h.update(tables[k].to_csv(index=False).encode())
    return h.hexdigest()


def test_same_seed_gives_identical_tables(starter):
    cfg, t = starter
    assert digest(generate_all(cfg)) == digest(t)


def test_different_seed_changes_the_data(starter):
    cfg, t = starter
    other = generate_all(build_config("starter", seed=cfg.seed + 1))
    assert digest(other) != digest(t)


def test_cli_files_are_byte_identical_across_runs(tmp_path):
    for name in ("a", "b"):
        assert main(["--scale", "starter", "--out", str(tmp_path / name)]) == 0
    files = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*") if p.is_file())
    assert {p.name for p in files} >= {"ledger_entry.csv", "receivable.csv", "daily_cash_position.csv",
                                       "consolidated_daily_position.csv", "settlement_link.csv"}
    for rel in files:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes(), rel


def test_other_stream_draws_do_not_change_transactions(starter):
    """The txn stream is independent: extra draws on another stream change nothing."""
    from src.master import generate_master
    from src.rng import RngStreams
    from src.txn import generate_transactions
    cfg, t = starter
    s = RngStreams(cfg.seed)
    m = generate_master(cfg, s)
    s.forecast.random(1000)
    s.anomaly.random(1000)
    again = generate_transactions(cfg, m, s)
    assert digest(again) == digest({k: t[k] for k in again})


# ---- documentation --------------------------------------------------------------------------------------
def test_every_transaction_parameter_is_documented():
    doc = (ROOT / "docs" / "distributions.md").read_text()
    names = [n for n in vars(P) if n.isupper()]
    assert names
    missing = [n for n in names if n not in doc]
    assert not missing, missing


# ---- suspended entities (audit H3) ----------------------------------------------------------------
def test_suspension_date_is_set_exactly_for_suspended_entities(built):
    cfg, t = built
    e = t["legal_entity"]
    assert ((e.status == "SUSPENDED") == e.suspended_from.notna()).all()
    assert (e.status == "SUSPENDED").any()
    s = e[e.status == "SUSPENDED"]
    assert (s.suspended_from > s.effective_from).all()
    assert s.suspended_from.between(cfg.date_start.isoformat(), cfg.date_end.isoformat()).all()


def test_a_suspended_entity_posts_nothing_from_its_suspension_date_but_did_before(built):
    cfg, t = built
    led = t["ledger_entry"].merge(t["bank_account"][["bank_account_id", "entity_id"]], on="bank_account_id")
    for r in t["legal_entity"].query("status == 'SUSPENDED'").itertuples():
        mine = led[led.entity_id == r.entity_id]
        assert len(mine) > 0, "a suspended entity should have traded before it was suspended"
        assert (mine.entry_date < r.suspended_from).all()


def test_suspended_accounts_keep_their_daily_positions_after_suspension(built):
    cfg, t = built
    e = t["legal_entity"].query("status == 'SUSPENDED'").iloc[0]
    accts = t["bank_account"][t["bank_account"].entity_id == e.entity_id].bank_account_id
    pos = t["daily_cash_position"]
    last = pos[pos.bank_account_id.isin(accts)].groupby("bank_account_id").position_date.max()
    assert (last == cfg.date_end.isoformat()).all()
    after = pos[pos.bank_account_id.isin(accts) & (pos.position_date >= e.suspended_from)]
    assert len(after) > 0 and (after.total_credits_minor == 0).all() and (after.total_debits_minor == 0).all()
    assert (after.opening_minor == after.closing_minor).all()
