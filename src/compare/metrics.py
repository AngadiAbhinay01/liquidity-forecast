"""Accuracy metrics computed from forecast_vs_actual (items) and forecast_daily_projection (days).

Sign convention everywhere: variance = actual - forecast, so a POSITIVE variance or bias means
more cash than forecast (inflows higher or outflows lower). Amounts are base-currency minor units.

Scored population: runs with status APPROVED. Superseded versions and unreviewed or
budget-limited runs are listed per run but not scored (see docs/distributions.md).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from ..txn.common import Calendar, RateBook


def _scores(var: np.ndarray, actual: np.ndarray, forecast: np.ndarray) -> dict:
    """var = actual - forecast (signed net cash)."""
    n = len(var)
    abs_actual = np.abs(actual).sum()
    nonzero = actual != 0
    return {
        "n": int(n),
        "mae_base_minor": float(np.abs(var).mean()) if n else None,
        "mape": float((np.abs(var[nonzero]) / np.abs(actual[nonzero])).mean()) if nonzero.any() else None,
        "wape": float(np.abs(var).sum() / abs_actual) if abs_actual else None,
        "accuracy": float(1 - np.abs(var).sum() / abs_actual) if abs_actual else None,
        "bias_base_minor": float(var.mean()) if n else None,
        "bias_pct_of_actual": float(var.sum() / abs_actual) if abs_actual else None,
    }


def item_population(fva: pd.DataFrame) -> pd.DataFrame:
    """Forecast items that fell due inside the comparison window, or were paid inside it."""
    f = fva[fva.cash_flow_forecast_id.notna()]
    due = f.forecast_date <= f.compared_through_date
    return f[due | (f.actual_amount_minor > 0)]


def run_metrics(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    fva, runs, sc = tables["forecast_vs_actual"], tables["forecast_run"], tables["liquidity_scenario"]
    runs = runs.merge(sc[["scenario_id", "scenario_code"]], on="scenario_id")
    rows = []
    items = item_population(fva)
    for r in runs.itertuples(index=False):
        it = items[items.forecast_run_id == r.forecast_run_id]
        allrows = fva[fva.forecast_run_id == r.forecast_run_id]
        s = _scores(it.variance_base_minor.to_numpy(), it.actual_net_base_minor.to_numpy(),
                    it.forecast_net_base_minor.to_numpy())
        t = _scores(allrows.variance_base_minor.to_numpy(), allrows.actual_net_base_minor.to_numpy(),
                    allrows.forecast_net_base_minor.to_numpy())
        rows.append({"forecast_run_id": r.forecast_run_id, "scenario_code": r.scenario_code,
                     "cutoff_date": r.cutoff_date, "version_no": r.version_no, "status": r.status,
                     "scored": r.status == "APPROVED",
                     **{f"item_{k}": v for k, v in s.items()},
                     **{f"all_flows_{k}": v for k, v in t.items()}})
    return pd.DataFrame(rows)


def scenario_metrics(tables: dict[str, pd.DataFrame], daily: pd.DataFrame | None = None) -> pd.DataFrame:
    """Accuracy by scenario over approved runs. ``item_*`` scores forecast items only;
    ``all_flows_*`` also counts actual flows nobody forecast (small documents, fees, ...)."""
    fva, runs, sc = tables["forecast_vs_actual"], tables["forecast_run"], tables["liquidity_scenario"]
    runs = runs.merge(sc[["scenario_id", "scenario_code"]], on="scenario_id")
    scored = runs[runs.status == "APPROVED"]
    items = item_population(fva)
    out = []
    for code in sc.scenario_code:
        ids_ = scored.forecast_run_id[scored.scenario_code == code]
        it = items[items.forecast_run_id.isin(ids_)]
        al = fva[fva.forecast_run_id.isin(ids_)]
        row = {"scenario_code": code, "runs_scored": int(len(ids_))}
        row.update({f"item_{k}": v for k, v in _scores(it.variance_base_minor.to_numpy(),
                                                        it.actual_net_base_minor.to_numpy(),
                                                        it.forecast_net_base_minor.to_numpy()).items()})
        row.update({f"all_flows_{k}": v for k, v in _scores(al.variance_base_minor.to_numpy(),
                                                             al.actual_net_base_minor.to_numpy(),
                                                             al.forecast_net_base_minor.to_numpy()).items()})
        d = daily[daily.forecast_run_id.isin(ids_)] if daily is not None else None
        if d is not None and len(d):
            row.update({f"daily_net_{k}": v for k, v in _scores(
                d.net_variance_base_minor.to_numpy(), d.actual_net_base_minor.to_numpy(),
                d.forecast_net_base_minor.to_numpy()).items() if k != "n"})
            row["daily_closing_mae_base_minor"] = float(d.closing_error_base_minor.abs().mean())
        out.append(row)
    return pd.DataFrame(out)


def driver_summary(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    fva, runs, sc = tables["forecast_vs_actual"], tables["forecast_run"], tables["liquidity_scenario"]
    runs = runs.merge(sc[["scenario_id", "scenario_code"]], on="scenario_id")
    f = fva.merge(runs[["forecast_run_id", "scenario_code", "status"]], on="forecast_run_id")
    f = f[f.status == "APPROVED"]
    g = f.groupby(["scenario_code", "variance_driver"]).agg(
        rows=("comparison_id", "size"), variance_base_minor=("variance_base_minor", "sum"),
        abs_variance_base_minor=("variance_base_minor", lambda s: int(s.abs().sum()))).reset_index()
    return g


def daily_variance(cfg: Config, tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Forecast versus actual cash per run, entity and day (base currency).

    Forecast = the projection's inflow/outflow. Actual = all ledger entries of the entity's
    accounts that day, valued at that day's rate. Closing error = projected closing minus the
    entity's actual closing (daily_cash_position valued that day); the projection values
    cash at the cutoff rate (and the scenario's shock), so FX drift shows up here."""
    cal = Calendar(cfg)
    proj = tables["forecast_daily_projection"]
    led = tables["ledger_entry"].merge(tables["bank_account"][["bank_account_id", "entity_id"]], on="bank_account_id")
    led["signed"] = np.where(led.direction == "CREDIT", led.base_amount_minor, -led.base_amount_minor)
    led["inflow"] = np.where(led.direction == "CREDIT", led.base_amount_minor, 0)
    led["outflow"] = np.where(led.direction == "DEBIT", led.base_amount_minor, 0)
    act = led.groupby(["entity_id", "entry_date"], as_index=False)[["inflow", "outflow"]].sum()
    pos = tables["daily_cash_position"].merge(tables["bank_account"][["bank_account_id", "entity_id"]],
                                              on="bank_account_id")
    close = pos.groupby(["entity_id", "position_date"], as_index=False).closing_base_minor.sum()
    d = proj.merge(act, how="left", left_on=["entity_id", "projection_date"], right_on=["entity_id", "entry_date"])
    d = d.merge(close, how="left", left_on=["entity_id", "projection_date"], right_on=["entity_id", "position_date"])
    d[["inflow", "outflow"]] = d[["inflow", "outflow"]].fillna(0).astype("int64")
    d["actual_closing_base_minor"] = d.closing_base_minor.fillna(0).astype("int64")
    out = pd.DataFrame({
        "forecast_run_id": d.forecast_run_id, "entity_id": d.entity_id, "date": d.projection_date,
        "forecast_inflow_base_minor": d.projected_inflow_base_minor,
        "forecast_outflow_base_minor": d.projected_outflow_base_minor,
        "actual_inflow_base_minor": d.inflow, "actual_outflow_base_minor": d.outflow})
    out["forecast_net_base_minor"] = out.forecast_inflow_base_minor - out.forecast_outflow_base_minor
    out["actual_net_base_minor"] = out.actual_inflow_base_minor - out.actual_outflow_base_minor
    out["net_variance_base_minor"] = out.actual_net_base_minor - out.forecast_net_base_minor
    out["projected_closing_base_minor"] = d.projected_closing_base_minor
    out["actual_closing_base_minor"] = d.actual_closing_base_minor
    out["closing_error_base_minor"] = out.projected_closing_base_minor - out.actual_closing_base_minor
    return out.sort_values(["forecast_run_id", "entity_id", "date"], kind="stable").reset_index(drop=True)


def accuracy_reports(cfg: Config, tables: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    daily = daily_variance(cfg, tables)
    return {"forecast_variance_daily": daily, "forecast_accuracy_by_run": run_metrics(tables),
            "forecast_accuracy_by_scenario": scenario_metrics(tables, daily), "variance_driver_summary": driver_summary(tables)}
