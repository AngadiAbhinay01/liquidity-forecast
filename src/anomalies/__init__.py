"""Anomaly injector. Runs AFTER the valid baseline exists, on its own random stream.

It receives the finished baseline tables, copies them, and changes only the rows it
targets. Everything it did is returned as a separate ground-truth table; the input
tables never carry a label. Five cases, one function each:

  1 OPENING_PLUS_LEDGER_NE_CLOSING   closing_minor of the last position row of an account is wrong
  2 FX_RATE_MISSING                  a non-base ledger entry loses fx_rate_id and base_amount_minor
  3 DOUBLE_COUNTED_CASH_FLOW         a forecast item is repeated in the same run (new id, same content)
  4 FORECAST_USES_ACTUAL_AFTER_CUTOFF  an item's source points to an actual dated after the run's cutoff
  5 CURRENCY_MIXED_WITHOUT_CONVERSION  an entry in a foreign-currency account is re-labelled USD, amount unchanged

Targets of different cases never overlap.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .. import ids
from ..config import Config
from ..rng import RngStreams
from . import params as P

GROUND_TRUTH_COLUMNS = ["record_id", "table", "issue_type", "description", "expected_decision",
                        "expected_reviewer_role", "injected_changes"]


def cases_per_type(cfg: Config) -> int:
    return max(P.ANOMALIES_PER_TYPE_MIN, round(P.ANOMALIES_PER_TYPE_FULL * cfg.n_ledger / 100_000))


def _truth(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=GROUND_TRUTH_COLUMNS)


def _row(issue: str, record_id: str, description: str, changes: dict) -> dict:
    table, decision, role = P.EXPECTED[issue]
    return {"record_id": record_id, "table": table, "issue_type": issue, "description": description,
            "expected_decision": decision, "expected_reviewer_role": role,
            "injected_changes": json.dumps(changes, sort_keys=True)}


def inject_anomalies(cfg: Config, baseline: dict[str, pd.DataFrame],
                     streams: RngStreams | None = None) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    rng = (streams or RngStreams(cfg.seed)).anomaly
    n = cases_per_type(cfg)
    t = {k: v.copy() for k, v in baseline.items()}
    truth: list[dict] = []
    used_led: set[str] = set()
    used_cf: set[str] = set()

    # 1. closing balance wrong on the final position row of n accounts -----------------------
    pos = t["daily_cash_position"]
    last = pos.sort_values(["bank_account_id", "position_date"]).groupby("bank_account_id").tail(1)
    size = pos.groupby("bank_account_id").size()
    eligible = last[last.bank_account_id.map(size) >= P.MIN_POSITION_DAYS]
    for i in rng.choice(len(eligible), n, replace=False):
        r = eligible.iloc[int(i)]
        idx = pos.index[(pos.bank_account_id == r.bank_account_id) & (pos.position_date == r.position_date)][0]
        delta = int(round(abs(r.closing_minor) * rng.uniform(*P.CLOSING_ERROR_FRACTION))) + P.CLOSING_ERROR_FLOOR_MINOR
        delta *= 1 if rng.random() < 0.5 else -1
        pos.loc[idx, "closing_minor"] = int(r.closing_minor) + delta
        truth.append(_row("OPENING_PLUS_LEDGER_NE_CLOSING", f"{r.bank_account_id}|{r.position_date}",
                          f"Stored closing balance is {delta:+d} minor units away from opening + credits - debits "
                          f"on the last position row of the account.",
                          {"closing_minor": {"from": int(r.closing_minor), "to": int(r.closing_minor) + delta}}))

    # 2. FX rate missing on n non-base ledger entries --------------------------------------------
    led = t["ledger_entry"]
    led["base_amount_minor"] = led["base_amount_minor"].astype("Int64")    # allows NULL, keeps integers
    foreign = led[(led.currency != cfg.base_currency.code) & led.fx_rate_id.notna()]
    for i in rng.choice(len(foreign), n, replace=False):
        r = foreign.iloc[int(i)]
        used_led.add(r.ledger_entry_id)
        m = led.ledger_entry_id == r.ledger_entry_id
        led.loc[m, ["fx_rate_id", "base_amount_minor"]] = [None, None]
        truth.append(_row("FX_RATE_MISSING", r.ledger_entry_id,
                          f"{r.currency} ledger entry has no FX rate and no base-currency amount.",
                          {"fx_rate_id": {"from": r.fx_rate_id, "to": None},
                           "base_amount_minor": {"from": int(r.base_amount_minor), "to": None}}))

    # 3. cash flow counted twice: repeat n forecast items in their own run ----------------------------
    cf = t["cash_flow_forecast"]
    factory = ids.IdFactory()
    year_max: dict[int, int] = {}
    for cid in cf.cash_flow_forecast_id:
        y = ids.year_of(cid)
        year_max[y] = max(year_max.get(y, 0), ids.sequence_of(cid))
    for y, mx in year_max.items():                           # continue after the highest existing counter
        for _ in range(mx):
            factory.cash_flow(y)
    runs = t["forecast_run"].set_index("forecast_run_id")
    doc_items = cf[cf.source_type != "RUN_RATE"]
    dupes = []
    for i in rng.choice(len(doc_items), n, replace=False):
        r = doc_items.iloc[int(i)]
        used_cf.add(r.cash_flow_forecast_id)
        new_id = factory.cash_flow(ids.year_of(r.cash_flow_forecast_id))
        copy = r.copy()
        copy["cash_flow_forecast_id"] = new_id
        dupes.append(copy)
        truth.append(_row("DOUBLE_COUNTED_CASH_FLOW", new_id,
                          f"Repeats item {r.cash_flow_forecast_id} in run {r.forecast_run_id}: same source "
                          f"document, date and amount, counted twice.",
                          {"repeats": r.cash_flow_forecast_id}))
    t["cash_flow_forecast"] = pd.concat([cf, pd.DataFrame(dupes).astype(cf.dtypes.to_dict())],
                                        ignore_index=True)
    cf = t["cash_flow_forecast"]

    # 4. forecast uses an actual dated after the cutoff ---------------------------------------------------
    cut = cf.forecast_run_id.map(runs.cutoff_date)
    hor = cf.forecast_run_id.map(runs.horizon_end_date)
    led_day = led.set_index("ledger_entry_id").entry_date
    by_acct = led.groupby("bank_account_id")
    chosen = 0
    for i in rng.permutation(len(cf)):
        if chosen == n:
            break
        r = cf.iloc[int(i)]
        if r.cash_flow_forecast_id in used_cf or r.cash_flow_forecast_id in {d.cash_flow_forecast_id for d in dupes}:
            continue
        if r.bank_account_id not in by_acct.groups:
            continue
        a = led.loc[by_acct.groups[r.bank_account_id]]
        later = a[(a.entry_date > cut.iloc[int(i)]) & (a.entry_date <= hor.iloc[int(i)])
                  & ~a.ledger_entry_id.isin(used_led)]
        if later.empty:
            continue
        e = later.iloc[int(rng.integers(0, len(later)))]
        idx = cf.index[cf.cash_flow_forecast_id == r.cash_flow_forecast_id][0]
        used_cf.add(r.cash_flow_forecast_id)
        cf.loc[idx, ["source_ledger_entry_id", "source_data_date"]] = [e.ledger_entry_id, e.entry_date]
        truth.append(_row("FORECAST_USES_ACTUAL_AFTER_CUTOFF", r.cash_flow_forecast_id,
                          f"Item depends on ledger entry {e.ledger_entry_id} dated {e.entry_date}, "
                          f"after the run's cutoff {cut.iloc[int(i)]}.",
                          {"source_ledger_entry_id": {"from": r.source_ledger_entry_id, "to": e.ledger_entry_id},
                           "source_data_date": {"from": r.source_data_date, "to": e.entry_date}}))
        chosen += 1
    if chosen < n:
        raise ValueError("not enough forecast items with a later actual to inject case 4")

    # 5. currency mixed without conversion -----------------------------------------------------------------
    acct_cur = t["bank_account"].set_index("bank_account_id").currency
    cand = led[(led.currency != cfg.base_currency.code) & ~led.ledger_entry_id.isin(used_led)]
    for i in rng.choice(len(cand), n, replace=False):
        r = cand.iloc[int(i)]
        used_led.add(r.ledger_entry_id)
        m = led.ledger_entry_id == r.ledger_entry_id
        led.loc[m, ["currency", "fx_rate_id", "base_amount_minor"]] = [
            cfg.base_currency.code, None, int(r.amount_minor)]
        truth.append(_row("CURRENCY_MIXED_WITHOUT_CONVERSION", r.ledger_entry_id,
                          f"Entry in a {acct_cur[r.bank_account_id]} account is labelled {cfg.base_currency.code} "
                          f"with the same minor-unit number and no conversion.",
                          {"currency": {"from": r.currency, "to": cfg.base_currency.code},
                           "fx_rate_id": {"from": r.fx_rate_id, "to": None},
                           "base_amount_minor": {"from": int(r.base_amount_minor), "to": int(r.amount_minor)}}))
    return t, _truth(truth)
