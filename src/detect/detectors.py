"""Detectors: find the five issue types from the data alone.

Each detector takes tables (DataFrames) and returns one row per problem record. Nothing in
this package reads ground truth, labels, or the injector; the only inputs are the dataset
tables. A detection has: issue_type, table_name, record_id, evidence, impact_base_minor
(best effort, from the currency_rate table), detected_by.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..validate.reconcile import reconcile

COLUMNS = ["issue_type", "table_name", "record_id", "evidence", "impact_base_minor", "detected_by"]


class Money:
    """Base-currency value of an amount using only the currency and currency_rate tables."""

    def __init__(self, tables: dict[str, pd.DataFrame]):
        cur = tables["currency"]
        self.base = cur.currency_code[cur.is_base == 1].iloc[0]
        self.exp = dict(zip(cur.currency_code, cur.minor_unit_exponent))
        r = tables["currency_rate"]
        self.rate = {(c, d): v for c, d, v in zip(r.quote_currency, r.rate_date, r.rate)}

    def base_minor(self, amount, currency: str, date: str):
        if currency == self.base:
            return int(abs(amount))
        rate = self.rate.get((currency, date))
        if rate is None:
            return None
        return int(round(abs(amount) * rate * 10.0 ** (self.exp[self.base] - self.exp[currency])))


def _frame(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


# 1 ----------------------------------------------------------------------------------------
def detect_position_breaks(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """opening + credits - debits != closing, a broken day-to-day chain, or totals that
    disagree with the ledger entries of that account and day."""
    money = Money(t)
    pos = t["daily_cash_position"]
    breaks = reconcile(t["ledger_entry"], pos, t["bank_account"])
    use = breaks[breaks.check.isin(["ARITHMETIC", "CHAIN", "CREDITS_VS_LEDGER", "DEBITS_VS_LEDGER", "COUNT_VS_LEDGER"])]
    cur = pos.set_index(["bank_account_id", "position_date"]).currency
    rows = []
    for (acct, day), g in use.groupby(["bank_account_id", "position_date"]):
        parts = [f"{r.check}: expected {int(r.expected)}, stored {int(r.actual)} (diff {int(r.diff):+d})"
                 for r in g.itertuples()]
        ar = g[g.check == "ARITHMETIC"]
        size = abs(int(ar["diff"].iloc[0])) if len(ar) else abs(int(g["diff"].iloc[0]))
        rows.append(("OPENING_PLUS_LEDGER_NE_CLOSING", "daily_cash_position", f"{acct}|{day}",
                     "; ".join(parts), money.base_minor(size, cur[(acct, day)], day), "position_reconciliation"))
    return _frame(rows)


# 2 ----------------------------------------------------------------------------------------
def detect_missing_fx(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """A non-base-currency row with no FX rate, no base amount, or a rate id that does not exist."""
    money = Money(t)
    rate_ids = set(t["currency_rate"].rate_id)
    rows = []
    specs = [("ledger_entry", "ledger_entry_id", "entry_date", "amount_minor", "base_amount_minor"),
             ("cash_flow_forecast", "cash_flow_forecast_id", "expected_date", "forecast_amount_minor",
              "forecast_base_amount_minor"),
             ("daily_cash_position", None, "position_date", "closing_minor", "closing_base_minor")]
    for table, idcol, datecol, amtcol, basecol in specs:
        df = t[table]
        df = df[df.currency != money.base]
        bad_ref = df.fx_rate_id.notna() & ~df.fx_rate_id.isin(rate_ids)
        bad = df.fx_rate_id.isna() | df[basecol].isna() | bad_ref
        for r in df[bad].itertuples(index=False):
            rid = getattr(r, idcol) if idcol else f"{r.bank_account_id}|{r.position_date}"
            why = ("rate id not found" if (pd.notna(r.fx_rate_id) and r.fx_rate_id not in rate_ids)
                   else "no FX rate" if pd.isna(r.fx_rate_id) else "no base amount")
            rows.append(("FX_RATE_MISSING", table, rid,
                         f"{r.currency} row: {why} (rate id {r.fx_rate_id}, base amount {getattr(r, basecol)})",
                         money.base_minor(getattr(r, amtcol), r.currency, getattr(r, datecol)),
                         "fx_completeness"))
    return _frame(rows)


# 3 ----------------------------------------------------------------------------------------
DUP_KEY = ["forecast_run_id", "bank_account_id", "source_type", "receivable_id", "payable_id",
           "source_ledger_entry_id", "direction", "currency", "expected_date", "forecast_date",
           "unadjusted_amount_minor", "forecast_amount_minor"]


def detect_double_counts(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """The same item repeated inside one run. The first (lowest id) is kept; each repeat is flagged."""
    cf = t["cash_flow_forecast"].sort_values("cash_flow_forecast_id", kind="stable")
    key = cf[DUP_KEY].astype(object).where(cf[DUP_KEY].notna(), "<none>").astype(str).agg("|".join, axis=1)
    first = cf.groupby(key.values).cash_flow_forecast_id.transform("first")
    rep = cf[cf.cash_flow_forecast_id != first.values]
    money = Money(t)
    rows = []
    for r, original in zip(rep.itertuples(index=False), first[rep.index]):
        rows.append(("DOUBLE_COUNTED_CASH_FLOW", "cash_flow_forecast", r.cash_flow_forecast_id,
                     f"Identical to {original} in run {r.forecast_run_id} (same source, dates and amounts)",
                     int(r.forecast_base_amount_minor) if pd.notna(r.forecast_base_amount_minor) else None,
                     "duplicate_forecast_items"))
    return _frame(rows)


# 4 ----------------------------------------------------------------------------------------
def detect_cutoff_leaks(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """An item whose source data, source ledger entry or FX rate is dated after its run's cutoff."""
    cf = t["cash_flow_forecast"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id")
    led = t["ledger_entry"].set_index("ledger_entry_id").entry_date
    cf["src_entry_date"] = cf.source_ledger_entry_id.map(led)
    rate_date = cf.fx_rate_id.str[8:].str.replace(r"(\d{4})(\d{2})(\d{2})", r"\1-\2-\3", regex=True)
    why = pd.Series("", index=cf.index)
    late_data = cf.source_data_date > cf.cutoff_date
    late_entry = cf.src_entry_date.notna() & (cf.src_entry_date > cf.cutoff_date)
    late_rate = rate_date.notna() & (rate_date > cf.cutoff_date)
    bad = cf[late_data | late_entry | late_rate]
    rows = []
    for i, r in bad.iterrows():
        parts = []
        if late_data[i]:
            parts.append(f"source_data_date {r.source_data_date} > cutoff {r.cutoff_date}")
        if late_entry[i]:
            parts.append(f"source ledger entry {r.source_ledger_entry_id} dated {r.src_entry_date} > cutoff {r.cutoff_date}")
        if late_rate[i]:
            parts.append(f"FX rate dated {rate_date[i]} > cutoff {r.cutoff_date}")
        rows.append(("FORECAST_USES_ACTUAL_AFTER_CUTOFF", "cash_flow_forecast", r.cash_flow_forecast_id,
                     "; ".join(parts), int(r.forecast_base_amount_minor), "forecast_cutoff_discipline"))
    return _frame(rows)


# 5 ----------------------------------------------------------------------------------------
def detect_currency_mix(t: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """A row whose currency differs from its bank account's currency."""
    money = Money(t)
    acct = t["bank_account"].set_index("bank_account_id").currency
    rows = []
    for table, idcol, datecol, amtcol in (
            ("ledger_entry", "ledger_entry_id", "entry_date", "amount_minor"),
            ("cash_flow_forecast", "cash_flow_forecast_id", "expected_date", "forecast_amount_minor"),
            ("receivable", "receivable_id", "invoice_date", "amount_minor"),
            ("payable", "payable_id", "invoice_date", "amount_minor")):
        df = t[table]
        want = df.bank_account_id.map(acct)
        for r, w in zip(df[df.currency != want].itertuples(index=False), want[df.currency != want]):
            # value the amount as the account currency it should have been (what the number really is)
            rows.append(("CURRENCY_MIXED_WITHOUT_CONVERSION", table, getattr(r, idcol),
                         f"row currency {r.currency} but account currency {w}",
                         money.base_minor(getattr(r, amtcol), w, getattr(r, datecol)), "currency_consistency"))
    return _frame(rows)


DETECTORS = (detect_position_breaks, detect_missing_fx, detect_double_counts, detect_cutoff_leaks,
             detect_currency_mix)


def detect_all(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = [d(tables) for d in DETECTORS]
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values(["issue_type", "table_name", "record_id"], kind="stable").reset_index(drop=True)
