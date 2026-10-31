"""Liquidity scenarios and the adjustment rule.

A scenario is four integers. ``adjust`` is the only code that applies them, and it
receives nothing else about the scenario (no code, no name, no id): two scenarios with
equal parameters cannot produce different forecasts. tests/test_forecast.py proves this.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..config import Config
from ..txn.common import Calendar, RateBook
from . import params as P


@dataclass(frozen=True)
class ScenarioParams:
    receipt_delay_days: int = 0
    collection_haircut_bps: int = 0
    fx_shock_bps: int = 0
    one_off_multiplier_bps: int = 10_000

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def scenario_table(cfg: Config) -> pd.DataFrame:
    rows = []
    for n, (code, name, delay, haircut, shock, mult) in enumerate(P.SCENARIO_DEFINITIONS, start=1):
        rows.append({"scenario_id": f"SCN-{n:03d}", "scenario_code": code, "version_no": 1,
                     "scenario_name": name, "assumption_notes": P.SCENARIO_NOTES[code],
                     "receipt_delay_days": delay, "collection_haircut_bps": haircut,
                     "fx_shock_bps": shock, "one_off_multiplier_bps": mult,
                     "status": "ACTIVE", "effective_from": cfg.date_start.isoformat(),
                     "effective_to": None})
    return pd.DataFrame(rows)


def params_of(row) -> ScenarioParams:
    return ScenarioParams(int(row["receipt_delay_days"]), int(row["collection_haircut_bps"]),
                          int(row["fx_shock_bps"]), int(row["one_off_multiplier_bps"]))


def adjust(cands: pd.DataFrame, params: ScenarioParams, cutoff_day: int,
           cal: Calendar, rb: RateBook) -> pd.DataFrame:
    """Apply the four parameters to scenario-independent candidates.

    delay      receivables only: forecast_day = next business day on/after base_day + delay
    haircut    receivables only: amount x (10000 - haircut) / 10000
    multiplier one-off payables only: amount x multiplier / 10000
    fx shock   non-base currencies: rate at the cutoff x (10000 + shock) / 10000
    Everything else is copied unchanged.
    """
    out = cands[["key", "base_day", "outstanding", "currency"]].copy()
    is_rcv = (cands.source_type == "RECEIVABLE").to_numpy()
    is_one_off = cands.is_one_off.to_numpy().astype(bool)

    day = cands.base_day.to_numpy() + np.where(is_rcv, params.receipt_delay_days, 0)
    wd = (cal.start.weekday() + day) % 7
    day = day + np.where(wd == 5, 2, np.where(wd == 6, 1, 0))
    out["forecast_day"] = day

    amount = cands.outstanding.to_numpy().astype(float)
    amount = np.where(is_rcv, amount * (10_000 - params.collection_haircut_bps) / 10_000, amount)
    amount = np.where(is_one_off, amount * params.one_off_multiplier_bps / 10_000, amount)
    out["forecast_amount"] = np.rint(amount).astype(np.int64)

    base = np.zeros(len(out), dtype=np.int64)
    for cur in out.currency.unique():
        m = (out.currency == cur).to_numpy()
        if cur == rb.base:
            base[m] = out.forecast_amount.to_numpy()[m]
            continue
        rate = float(rb.rate(cur, cutoff_day)) * (10_000 + params.fx_shock_bps) / 10_000
        scale = 10.0 ** (rb.exp[rb.base] - rb.exp[cur])
        base[m] = np.rint(out.forecast_amount.to_numpy()[m] * rate * scale).astype(np.int64)
    out["forecast_base"] = base
    return out
