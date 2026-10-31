"""Forecast stage: scenarios, versioned runs, cash-flow items, projected daily liquidity."""
from __future__ import annotations

import json

import pandas as pd

from .. import ids
from ..config import Config
from ..rng import RngStreams
from ..txn.common import Calendar, RateBook
from . import params as P
from .candidates import Knowledge
from .runs import (ITEM_COLUMNS, build_items, checksum, month_end_cutoffs, plan_runs, projection,
                   run_events)
from .scenarios import params_of, scenario_table


def generate_forecasts(cfg: Config, tables: dict[str, pd.DataFrame],
                       streams: RngStreams | None = None, reserve: int = 0) -> dict[str, pd.DataFrame]:
    """``reserve`` items are left out of the budget so that the published total, after the injector
    appends that many repeated items, equals ``cfg.n_forecast_items`` exactly."""
    streams = streams or RngStreams(cfg.seed)
    rng = streams.forecast
    cal = Calendar(cfg)
    rb = RateBook(cfg, tables["currency_rate"], cal)
    know = Knowledge(cal, rb, tables, cfg.n_days)
    H = cfg.forecast_horizon_days

    scenarios = scenario_table(cfg)
    sparams = [params_of(r) for _, r in scenarios.iterrows()]
    months = month_end_cutoffs(cfg, cal)
    cands = {c: know.candidates(c, H) for c in months}
    plans = plan_runs({c: len(v) for c, v in cands.items()}, months, len(scenarios), cfg.n_forecast_items - reserve)
    plans.sort(key=lambda p: (p.cutoff, p.version, p.scenario_idx))
    # run_seq per cutoff in creation order: scenarios first, re-runs after
    factory = ids.IdFactory()
    for p in plans:
        p.run_id = factory.forecast_run(cal.date(p.cutoff))

    cf_factory = ids.IdFactory()
    runs, events, items_out, proj_out = [], [], [], []
    event_id = 0
    for p in plans:
        c, sp = p.cutoff, sparams[p.scenario_idx]
        items = build_items(cands[c], sp, c, cal, rb, p.max_items)
        items.insert(0, "forecast_run_id", p.run_id)
        items.insert(0, "cash_flow_forecast_id",
                     [cf_factory.cash_flow(cal.date(c).year) for _ in range(len(items))])
        proj_out.append(projection(items, know, tables["legal_entity"], c, H, sp, p.run_id, cal, rb))
        items_out.append(items)

        superseded_by_rerun = any(q.rerun_of is p for q in plans)
        if superseded_by_rerun:
            path = [("CREATED", c), ("RUNNING", c), ("COMPLETED", c), ("REJECTED", c + P.REVIEW_LAG_DAYS),
                    ("SUPERSEDED", c + P.REVIEW_LAG_DAYS)]
        elif p.rerun_of is not None:
            d0 = c + P.REVIEW_LAG_DAYS
            path = [("CREATED", d0), ("RUNNING", d0), ("COMPLETED", d0), ("APPROVED", d0 + P.REVIEW_LAG_DAYS)]
        else:
            path = [("CREATED", c), ("RUNNING", c), ("COMPLETED", c)]
            if p.max_items is None and rng.random() < P.APPROVED_SHARE:
                path.append(("APPROVED", c + P.REVIEW_LAG_DAYS))
        evs = run_events(path)
        for ev in evs:
            event_id += 1
            events.append({"event_id": event_id, "forecast_run_id": p.run_id, **{
                k: v for k, v in ev.items() if k != "event_day"}, "event_date": cal.iso(ev["event_day"])})
        sc = scenarios.iloc[p.scenario_idx]
        runs.append({
            "forecast_run_id": p.run_id, "scenario_id": sc.scenario_id, "cutoff_date": cal.iso(c),
            "run_seq": ids.sequence_of(p.run_id), "version_no": p.version,
            "supersedes_run_id": p.rerun_of.run_id if p.rerun_of else None, "horizon_days": H,
            "horizon_end_date": cal.iso(c + H), "base_currency": cfg.base_currency.code,
            "status": evs[-1]["to_status"], "scenario_params_json": sp.to_json(),
            "run_checksum": checksum(items)})

    items_df = pd.concat(items_out, ignore_index=True)[ITEM_COLUMNS]
    return {"liquidity_scenario": scenarios, "forecast_run": pd.DataFrame(runs),
            "forecast_run_status_event": pd.DataFrame(events), "cash_flow_forecast": items_df,
            "forecast_daily_projection": pd.concat(proj_out, ignore_index=True)}
