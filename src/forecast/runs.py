"""Forecast runs: planning, items, daily projections, versions and run history."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import ids
from ..config import Config
from ..txn.common import Calendar, RateBook
from . import params as P
from .candidates import Knowledge
from .scenarios import ScenarioParams, adjust, params_of

ITEM_COLUMNS = ["cash_flow_forecast_id", "forecast_run_id", "bank_account_id", "source_type", "receivable_id",
                "payable_id", "source_ledger_entry_id", "source_data_date", "direction", "currency",
                "expected_date", "forecast_date", "unadjusted_amount_minor", "forecast_amount_minor",
                "fx_rate_id", "forecast_base_amount_minor"]
CHECKSUM_COLUMNS = ITEM_COLUMNS[2:]


@dataclass
class RunPlan:
    cutoff: int
    scenario_idx: int
    version: int = 1
    max_items: int | None = None        # exposure-limited run: only the largest items
    rerun_of: "RunPlan | None" = None
    run_id: str = ""


def month_end_cutoffs(cfg: Config, cal: Calendar) -> list[int]:
    """Last business day of each month that leaves a full horizon after it."""
    out, y, m = [], cal.start.year, cal.start.month
    last_ok = (cfg.last_cutoff_date - cal.start).days
    while True:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = (date(ny, nm, 1) - timedelta(days=1) - cal.start).days
        c = cal.prev_bd(end)
        if c > last_ok or c > cfg.n_days - 1:
            break
        if c >= P.RUN_RATE_MIN_HISTORY_DAYS:
            out.append(c)
        y, m = ny, nm
    return out


def _evenly(n_total: int, k: int) -> list[int]:
    if k == 1:
        return [n_total // 2]
    return sorted({round(j * (n_total - 1) / (k - 1)) for j in range(k)})


def plan_runs(sizes: dict[int, int], months: list[int], n_scenarios: int, budget: int) -> list[RunPlan]:
    """Choose cutoffs and runs so the total number of items is exactly ``budget``.

    1. Take the largest number K of evenly spaced month-end cutoffs for which
       (all scenarios at each) + (one BASE re-run at cutoff index BASE_RERUN_AT_CUTOFF_INDEX)
       fits in the budget.
    2. Spend what is left at one more cutoff: BASE, DELAYED_RECEIPT, STRESS in turn, the
       last one limited to the largest items so the total hits the budget exactly."""
    def cost(sel):
        total = n_scenarios * sum(sizes[months[i]] for i in sel)
        if sel:
            total += sizes[months[sel[min(P.BASE_RERUN_AT_CUTOFF_INDEX, len(sel) - 1)]]]
        return total

    chosen: list[int] = []
    for k in range(len(months), 0, -1):
        sel = _evenly(len(months), k)
        if cost(sel) <= budget:
            chosen = sel
            break
    plans: list[RunPlan] = []
    for pos, i in enumerate(chosen):
        c = months[i]
        for s in range(n_scenarios):
            plans.append(RunPlan(c, s))
        if pos == min(P.BASE_RERUN_AT_CUTOFF_INDEX, len(chosen) - 1):
            first = [p for p in plans if p.cutoff == c and p.scenario_idx == 0][0]
            plans.append(RunPlan(c, 0, version=2, rerun_of=first))
    left = budget - cost(chosen)
    spare = [i for i in range(len(months)) if i not in chosen]
    if left > 0:
        if not spare:
            raise ValueError("forecast budget larger than all month-end cutoffs can supply")
        far = max(spare, key=lambda i: min((abs(i - j) for j in chosen), default=0))
        c = months[far]
        for s in range(n_scenarios):
            n = sizes[c]
            if left <= 0:
                break
            if left >= n:
                plans.append(RunPlan(c, s))
                left -= n
            else:
                plans.append(RunPlan(c, s, max_items=left))
                left = 0
        if left > 0:
            raise ValueError("could not spend the whole forecast item budget")
    return plans


def _iso(cal: Calendar, d: int) -> str:
    return cal.iso(d)


def build_items(cands: pd.DataFrame, params: ScenarioParams, c: int, cal: Calendar, rb: RateBook,
                max_items: int | None) -> pd.DataFrame:
    """Items for one run. Which candidates are used never depends on the scenario."""
    if max_items is not None:
        order = cands.sort_values(["usd_size", "key"], ascending=[False, True], kind="stable")
        cands = cands.loc[order.index[:max_items]].sort_values("key", kind="stable")
    adj = adjust(cands, params, c, cal, rb)
    items = pd.DataFrame({
        "bank_account_id": cands.account_id.to_numpy(), "source_type": cands.source_type.to_numpy(),
        "receivable_id": cands.receivable_id.to_numpy(), "payable_id": cands.payable_id.to_numpy(),
        "source_ledger_entry_id": cands.source_ledger_entry_id.to_numpy(),
        "source_data_date": [_iso(cal, d) for d in cands.source_data_day],
        "direction": cands.direction.to_numpy(), "currency": cands.currency.to_numpy(),
        "expected_date": [_iso(cal, d) for d in cands.expected_day],
        "forecast_date": [_iso(cal, d) for d in adj.forecast_day],
        "unadjusted_amount_minor": cands.outstanding.to_numpy(),
        "forecast_amount_minor": adj.forecast_amount.to_numpy(),
        "fx_rate_id": [rb.rate_id(cur, c) for cur in cands.currency],
        "forecast_base_amount_minor": adj.forecast_base.to_numpy(),
        "_entity": cands.entity_id.to_numpy(), "_forecast_day": adj.forecast_day.to_numpy(),
        "_key": cands.key.to_numpy()})
    return items.reset_index(drop=True)


def checksum(items: pd.DataFrame) -> str:
    body = items[CHECKSUM_COLUMNS].sort_values(CHECKSUM_COLUMNS[:6], kind="stable")
    return hashlib.sha256(body.to_csv(index=False, lineterminator="\n").encode()).hexdigest()


def projection(items: pd.DataFrame, know: Knowledge, entities: pd.DataFrame, c: int, horizon: int,
               params: ScenarioParams, run_id: str, cal: Calendar, rb: RateBook) -> pd.DataFrame:
    """Projected liquidity per entity and day, from the run's items (base currency).

    Opening cash = each live account's closing balance at the cutoff, valued at the cutoff
    rate with the scenario's FX shock. Flows are the items' forecast_base amounts on their
    forecast date; items dated after the horizon are outside the projection."""
    pos = know.opening_positions(c)
    open_base: dict[str, int] = {}
    for r in pos.itertuples(index=False):
        if r.currency == rb.base:
            v = int(r.closing_minor)
        else:
            rate = float(rb.rate(r.currency, c)) * (10_000 + params.fx_shock_bps) / 10_000
            v = int(np.rint(r.closing_minor * rate * 10.0 ** (rb.exp[rb.base] - rb.exp[r.currency])))
        open_base[r.entity_id] = open_base.get(r.entity_id, 0) + v
    buffer = entities.set_index("entity_id").min_liquidity_buffer_base_minor
    inside = items[(items._forecast_day > c) & (items._forecast_day <= c + horizon)]
    inflow = inside[inside.direction == "CREDIT"].groupby(["_entity", "_forecast_day"]).forecast_base_amount_minor.sum()
    outflow = inside[inside.direction == "DEBIT"].groupby(["_entity", "_forecast_day"]).forecast_base_amount_minor.sum()
    rows = []
    days = np.arange(c + 1, c + horizon + 1)
    for e in sorted(open_base):
        i = np.array([inflow.get((e, d), 0) for d in days], dtype=np.int64)
        o = np.array([outflow.get((e, d), 0) for d in days], dtype=np.int64)
        closing = open_base[e] + np.cumsum(i - o)
        opening = np.concatenate(([open_base[e]], closing[:-1]))
        buf = int(buffer[e])
        for k, d in enumerate(days):
            rows.append((run_id, e, _iso(cal, int(d)), int(opening[k]), int(i[k]), int(o[k]), int(closing[k]),
                         buf, int(closing[k] < buf)))
    return pd.DataFrame(rows, columns=["forecast_run_id", "entity_id", "projection_date",
                                       "opening_base_minor", "projected_inflow_base_minor",
                                       "projected_outflow_base_minor", "projected_closing_base_minor",
                                       "liquidity_buffer_base_minor", "shortfall_flag"])


RUN_TRANSITIONS = {
    None: {"CREATED"}, "CREATED": {"RUNNING"}, "RUNNING": {"COMPLETED", "FAILED"},
    "COMPLETED": {"APPROVED", "REJECTED", "SUPERSEDED"}, "APPROVED": {"SUPERSEDED"},
    "REJECTED": {"SUPERSEDED"},
}
_CAUSE = {"CREATED": ("CREATED", "SYSTEM"), "RUNNING": ("STARTED", "SYSTEM"),
          "COMPLETED": ("FINISHED", "SYSTEM"), "APPROVED": ("REVIEW_APPROVED", "RISK_COMPLIANCE_REVIEWER"),
          "REJECTED": ("REVIEW_REJECTED", "RISK_COMPLIANCE_REVIEWER"),
          "SUPERSEDED": ("SUPERSEDED", "SYSTEM")}


def run_events(path: list[tuple[str, int]]) -> list[dict]:
    """[(to_status, calendar day offset)] -> event rows, with every transition checked."""
    out, prev = [], None
    for to, day in path:
        if to not in RUN_TRANSITIONS[prev]:
            raise ValueError(f"illegal run transition {prev}->{to}")
        cause, actor = _CAUSE[to]
        out.append({"event_seq": len(out) + 1, "from_status": prev, "to_status": to,
                    "event_day": day, "cause": cause, "actor_role": actor})
        prev = to
    return out
