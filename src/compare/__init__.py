"""Actual-versus-forecast comparison.

For every forecast run the actuals dated after its cutoff, up to its horizon end, are
matched to the run's items and the variance is explained from the source records:
no driver is drawn at random. Rules and priority are in docs/distributions.md.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .. import ids
from ..config import Config
from ..txn.common import Calendar, RateBook

B = 10   # run-rate block length; must equal forecast.params.RUN_RATE_BLOCK_DAYS
OP_TYPES = ("OPERATING_INFLOW", "OPERATING_OUTFLOW")
COLUMNS = ["comparison_id", "forecast_run_id", "cash_flow_forecast_id", "actual_ledger_entry_id",
           "compared_through_date", "direction", "currency", "forecast_date", "actual_date",
           "date_variance_days", "forecast_amount_minor", "actual_amount_minor", "forecast_net_base_minor",
           "actual_net_base_minor", "variance_base_minor", "amount_variance_base_minor",
           "fx_variance_base_minor", "variance_driver"]


def _base(rb: RateBook, amount: pd.Series, cur: pd.Series, day: pd.Series) -> np.ndarray:
    out = np.zeros(len(amount), dtype=np.int64)
    for c in cur.unique():
        m = (cur == c).to_numpy()
        out[m] = rb.to_base_minor(amount.to_numpy()[m], c, day.to_numpy()[m])
    return out


def generate_comparisons(cfg: Config, tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    from ..forecast import params as FP
    assert B == FP.RUN_RATE_BLOCK_DAYS
    cal = Calendar(cfg)
    rb = RateBook(cfg, tables["currency_rate"], cal)
    off = cal.offset
    led = tables["ledger_entry"].assign(day=lambda d: d.entry_date.map(off))
    links = tables["settlement_link"]
    links = links.assign(doc_id=links.receivable_id.fillna(links.payable_id))
    links = links.merge(led[["ledger_entry_id", "bank_account_id", "day", "currency", "direction"]],
                        on="ledger_entry_id")
    links["alloc_base"] = _base(rb, links.allocated_amount_minor, links.currency, links.day)
    one_off = set(tables["payable"].payable_id[tables["payable"].is_one_off == 1])
    cf = tables["cash_flow_forecast"]
    runs = tables["forecast_run"]
    factory = ids.IdFactory()
    out = []

    for run in runs.sort_values("forecast_run_id").itertuples(index=False):
        c, T = off(run.cutoff_date), off(run.horizon_end_date)
        params = json.loads(run.scenario_params_json)
        items = cf[cf.forecast_run_id == run.forecast_run_id].reset_index(drop=True)
        sign = np.where(items.direction == "CREDIT", 1, -1)
        win_l = led[(led.day > c) & (led.day <= T)]
        win_k = links[(links.day > c) & (links.day <= T)]

        a_amt = np.zeros(len(items), dtype=np.int64)
        a_base = np.zeros(len(items), dtype=np.int64)
        a_day = np.full(len(items), -1, dtype=np.int64)
        a_led = np.full(len(items), None, dtype=object)

        # documents: payments linked to the document inside the window
        doc_id = items.receivable_id.fillna(items.payable_id)
        is_doc = doc_id.notna().to_numpy()
        if is_doc.any():
            g = (win_k.sort_values(["day", "ledger_entry_id"])
                 .groupby("doc_id").agg(amt=("allocated_amount_minor", "sum"), base=("alloc_base", "sum"),
                                        day=("day", "first"), led=("ledger_entry_id", "first")))
            m = doc_id[is_doc].map(g.amt)
            idx = np.flatnonzero(is_doc)
            has = m.notna().to_numpy()
            a_amt[idx[has]] = g.amt.reindex(doc_id[is_doc][has]).to_numpy()
            a_base[idx[has]] = g.base.reindex(doc_id[is_doc][has]).to_numpy()
            a_day[idx[has]] = g.day.reindex(doc_id[is_doc][has]).to_numpy()
            a_led[idx[has]] = g.led.reindex(doc_id[is_doc][has]).to_numpy()

        # run-rate items: regular flows of the same account and direction inside the block
        rr = (items.source_type == "RUN_RATE").to_numpy()
        matched_entries: set[str] = set()
        if rr.any():
            ri = items[rr].assign(_i=np.flatnonzero(rr))
            blk = (ri.expected_date.map(off) - c - 1) // B
            ri["ws"] = c + 1 + B * blk
            ri["we"] = np.minimum(c + B * (blk + 1), T)
            ops = win_l[win_l.entry_type.isin(OP_TYPES)][["ledger_entry_id", "bank_account_id", "direction", "day",
                                                          "amount_minor", "base_amount_minor"]]
            mm = ri.merge(ops, on=["bank_account_id", "direction"])
            mm = mm[(mm.day >= mm.ws) & (mm.day <= mm.we)]
            matched_entries = set(mm.ledger_entry_id)
            g = mm.sort_values(["day", "ledger_entry_id"]).groupby("_i").agg(
                amt=("amount_minor", "sum"), base=("base_amount_minor", "sum"),
                day=("day", "first"), led=("ledger_entry_id", "first"))
            a_amt[g.index.to_numpy()] = g.amt.to_numpy()
            a_base[g.index.to_numpy()] = g.base.to_numpy()
            a_day[g.index.to_numpy()] = g.day.to_numpy()
            a_led[g.index.to_numpy()] = g.led.to_numpy()

        f_day = items.forecast_date.map(off).to_numpy()
        have = a_amt > 0
        date_var = np.where(have, a_day - f_day, 0)
        rate_f = np.array([1.0 if cur == rb.base else float(rb.rate(cur, c)) * (10_000 + params["fx_shock_bps"]) / 10_000
                           for cur in items.currency])
        scale = np.array([10.0 ** (rb.exp[rb.base] - rb.exp[cur]) for cur in items.currency])
        f_net = sign * items.forecast_base_amount_minor.to_numpy()
        a_net = sign * a_base
        var = a_net - f_net
        amt_var = sign * np.rint((a_amt - items.forecast_amount_minor.to_numpy()) * rate_f * scale).astype(np.int64)
        fx_var = var - amt_var
        short = is_doc & (a_amt < items.unadjusted_amount_minor.to_numpy())
        driver = np.select(
            [~have, short, is_doc & (date_var > 0), is_doc & (date_var < 0),
             a_amt != items.forecast_amount_minor.to_numpy(), fx_var != 0],
            ["NOT_YET_SETTLED", "PARTIAL_OR_SHORT_PAYMENT", "TIMING_LATE", "TIMING_EARLY",
             np.where(is_doc, "SCENARIO_AMOUNT_ADJUSTMENT", "RUN_RATE_DEVIATION"), "FX_MOVEMENT"],
            default="ON_TARGET")
        forecasted = pd.DataFrame({
            "forecast_run_id": run.forecast_run_id, "cash_flow_forecast_id": items.cash_flow_forecast_id,
            "actual_ledger_entry_id": a_led, "direction": items.direction, "currency": items.currency,
            "forecast_date": items.forecast_date,
            "actual_date": [cal.iso(d) if h else None for d, h in zip(a_day, have)],
            "date_variance_days": [int(v) if h else None for v, h in zip(date_var, have)],
            "forecast_amount_minor": items.forecast_amount_minor, "actual_amount_minor": a_amt,
            "forecast_net_base_minor": f_net, "actual_net_base_minor": a_net, "variance_base_minor": var,
            "amount_variance_base_minor": amt_var, "fx_variance_base_minor": fx_var,
            "variance_driver": driver})

        # unforecast actuals: payments of documents the run did not forecast ...
        covered = set(doc_id.dropna())
        uk = win_k[~win_k.doc_id.isin(covered)]
        parts = [pd.DataFrame({
            "actual_ledger_entry_id": uk.ledger_entry_id, "direction": uk.direction, "currency": uk.currency,
            "actual_date": [cal.iso(d) for d in uk.day], "actual_amount_minor": uk.allocated_amount_minor,
            "actual_base": uk.alloc_base,
            "variance_driver": np.where(uk.doc_id.isin(one_off), "UNFORECAST_ONE_OFF", "UNFORECAST_OTHER")})]
        # ... and regular flows no run-rate block covered, plus fees, interest and reversals
        other = win_l[~win_l.entry_type.isin(["RECEIPT_SETTLEMENT", "PAYMENT_SETTLEMENT"])]
        other = other[~other.ledger_entry_id.isin(matched_entries)]
        parts.append(pd.DataFrame({
            "actual_ledger_entry_id": other.ledger_entry_id, "direction": other.direction,
            "currency": other.currency, "actual_date": [cal.iso(d) for d in other.day],
            "actual_amount_minor": other.amount_minor, "actual_base": other.base_amount_minor,
            "variance_driver": "UNFORECAST_OTHER"}))
        un = pd.concat(parts, ignore_index=True)
        if len(un):
            s = np.where(un.direction == "CREDIT", 1, -1)
            r_un = np.array([1.0 if cur == rb.base else float(rb.rate(cur, c)) * (10_000 + params["fx_shock_bps"]) / 10_000
                             for cur in un.currency])
            sc_un = np.array([10.0 ** (rb.exp[rb.base] - rb.exp[cur]) for cur in un.currency])
            u_net = s * un.actual_base.to_numpy()
            u_amt = s * np.rint(un.actual_amount_minor.to_numpy() * r_un * sc_un).astype(np.int64)
            un_rows = pd.DataFrame({
                "forecast_run_id": run.forecast_run_id, "cash_flow_forecast_id": None,
                "actual_ledger_entry_id": un.actual_ledger_entry_id, "direction": un.direction,
                "currency": un.currency, "forecast_date": None, "actual_date": un.actual_date,
                "date_variance_days": None, "forecast_amount_minor": 0,
                "actual_amount_minor": un.actual_amount_minor, "forecast_net_base_minor": 0,
                "actual_net_base_minor": u_net, "variance_base_minor": u_net,
                "amount_variance_base_minor": u_amt, "fx_variance_base_minor": u_net - u_amt,
                "variance_driver": un.variance_driver})
            un_rows = un_rows.sort_values(["actual_date", "actual_ledger_entry_id", "variance_driver"], kind="stable")
            forecasted = pd.concat([forecasted, un_rows], ignore_index=True)
        forecasted.insert(1, "compared_through_date", run.horizon_end_date)
        out.append(forecasted)

    df = pd.concat(out, ignore_index=True)
    year = df.compared_through_date.str[:4].astype(int)
    df.insert(0, "comparison_id", [factory.comparison(y) for y in year])
    return {"forecast_vs_actual": df[COLUMNS]}
