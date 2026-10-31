"""CurrencyRate generation.

Base currency: USD (``Config.base_currency``). Rates are quoted as base units per
1 unit of the quote currency (USD per EUR), so ``base_amount = amount * rate``.
USD is the base because it is the usual group reporting currency and keeps the
four stored pairs (EUR, GBP, INR, JPY vs USD) independent; cross rates are
derived through USD and never stored.

Model, per non-base currency c, on calendar day t (x = ln rate):

    weekday:  x[t] = x[t-1] + kappa * (line[t-1] - x[t-1]) + mu_c
                             + sigma_c * m[t] * eps[t] + shock[t]
    weekend:  x[t] = x[t-1]            (markets closed: Friday's rate carried forward)

    eps ~ N(0, 1)                      line[t] = ln(initial_c) + mu_c * (weekdays elapsed)
    m[t] = 1 + boost * sum_s exp(-(t - s) / tau) over earlier shock days s  (vol clustering)
    shock[t] = direction_c * beta_c * U(2%, 4%) on the market-wide shock days, else 0

Shock days are market-wide (all currencies on the same dates), weekdays only, at
least 20 days apart and not within 10 days of either end of the calendar. Rows
on a shock day carry ``rate_source = SYNTHETIC_SHOCK``, so a later FX-driven
forecast variance can be traced to a rule rather than assigned.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import ids
from ..config import Config
from . import params as P

COLUMNS = ["rate_id", "rate_date", "quote_currency", "base_currency", "rate", "rate_source"]


def calendar(cfg: Config) -> list[date]:
    return [cfg.date_start + timedelta(days=i) for i in range(cfg.n_days)]


def shock_day_indices(cfg: Config, rng: np.random.Generator) -> list[int]:
    """Indices into the calendar of the market-wide shock days (sorted)."""
    n_days = cfg.n_days
    n_shocks = max(1, round(n_days / P.SHOCK_EVERY_DAYS))
    days = calendar(cfg)
    candidates = [i for i in range(P.SHOCK_EDGE_DAYS, n_days - P.SHOCK_EDGE_DAYS)
                  if days[i].weekday() < 5]
    chosen: list[int] = []
    for _ in range(10_000):
        if len(chosen) == n_shocks:
            break
        i = candidates[int(rng.integers(0, len(candidates)))]
        if all(abs(i - j) >= P.SHOCK_MIN_SPACING_DAYS for j in chosen):
            chosen.append(i)
    if len(chosen) < n_shocks:
        raise ValueError("calendar too short to place the required FX shock days")
    return sorted(chosen)


def generate_currency_rates(cfg: Config, rng: np.random.Generator) -> pd.DataFrame:
    base = cfg.base_currency.code
    quotes = [c.code for c in cfg.currencies if not c.is_base]
    missing = [q for q in quotes if q not in P.INITIAL_RATE]
    if missing:
        raise ValueError(f"no FX parameters for currencies {missing}; add them to master/params.py")

    days = calendar(cfg)
    n = len(days)
    is_weekday = np.array([d.weekday() < 5 for d in days])
    shocks = shock_day_indices(cfg, rng)
    weekdays_elapsed = np.cumsum(is_weekday)

    rows = []
    for q in quotes:
        eps = rng.standard_normal(n)
        sizes = rng.uniform(P.SHOCK_SIZE[0], P.SHOCK_SIZE[1], len(shocks))

        shock_term = np.zeros(n)
        vol_mult = np.ones(n)
        for s, size in zip(shocks, sizes):
            shock_term[s] = P.SHOCK_DIRECTION[q] * P.SHOCK_BETA[q] * size
            k = np.arange(1, n - s)
            vol_mult[s + 1:] += P.POST_SHOCK_VOL_BOOST * np.exp(-k / P.POST_SHOCK_DECAY_DAYS)

        x0 = np.log(P.INITIAL_RATE[q])
        line = x0 + P.DAILY_DRIFT[q] * weekdays_elapsed
        x = np.empty(n)
        x[0] = x0
        for t in range(1, n):
            if not is_weekday[t]:
                x[t] = x[t - 1]
                continue
            x[t] = (x[t - 1]
                    + P.MEAN_REVERSION * (line[t - 1] - x[t - 1])
                    + P.DAILY_DRIFT[q]
                    + P.DAILY_VOL[q] * vol_mult[t] * eps[t]
                    + shock_term[t])
        rates = np.round(np.exp(x), P.FX_DECIMALS)

        shock_set = set(shocks)
        for t, d in enumerate(days):
            rows.append((ids.format_currency_rate_id(q, d), d.isoformat(), q, base,
                         float(rates[t]),
                         "SYNTHETIC_SHOCK" if t in shock_set else "SYNTHETIC_RANDOM_WALK"))

    df = pd.DataFrame(rows, columns=COLUMNS)
    return df.sort_values(["rate_date", "quote_currency"], kind="stable").reset_index(drop=True)


def rate_lookup(rates: pd.DataFrame, base: str) -> pd.DataFrame:
    """Wide table: index = ISO date, one column per currency, base fixed at 1.0."""
    wide = rates.pivot(index="rate_date", columns="quote_currency", values="rate")
    wide[base] = 1.0
    return wide
