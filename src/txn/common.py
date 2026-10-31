"""Calendar and FX helpers shared by the transaction stage.

Dates inside the stage are integer day offsets from ``Config.date_start`` (day 0 is
the first day of the window; pre-window invoices have negative offsets). Strings
are produced only when tables are built.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import ids
from ..config import Config


class Calendar:
    def __init__(self, cfg: Config):
        self.start: date = cfg.date_start
        self.n_days: int = cfg.n_days
        self._wd0: int = cfg.date_start.weekday()

    def is_bd(self, d: int) -> bool:
        return (self._wd0 + d) % 7 < 5

    def next_bd(self, d: int) -> int:
        """First business day on or after ``d``."""
        while not self.is_bd(d):
            d += 1
        return d

    def prev_bd(self, d: int) -> int:
        """Last business day on or before ``d``."""
        while not self.is_bd(d):
            d -= 1
        return d

    def iso(self, d: int) -> str:
        return (self.start + timedelta(days=int(d))).isoformat()

    def offset(self, iso: str) -> int:
        return (date.fromisoformat(iso) - self.start).days

    def date(self, d: int) -> date:
        return self.start + timedelta(days=int(d))


class RateBook:
    """Daily FX rates (base units per 1 unit of the currency) indexed by day offset.
    Days before the window use day 0's rate; weekend rates are already carried forward."""

    def __init__(self, cfg: Config, rates: pd.DataFrame, cal: Calendar):
        self.base = cfg.base_currency.code
        self.exp = {c.code: c.minor_unit_exponent for c in cfg.currencies}
        self.cal = cal
        wide = rates.pivot(index="rate_date", columns="quote_currency", values="rate").sort_index()
        if len(wide) != cfg.n_days:
            raise ValueError("currency_rate does not cover every calendar day")
        self._rate = {c: wide[c].to_numpy(dtype=float) for c in wide.columns}
        self._rate[self.base] = np.ones(cfg.n_days)

    def rate(self, cur: str, day) -> np.ndarray | float:
        arr = self._rate[cur]
        return arr[np.clip(day, 0, len(arr) - 1)]

    def rate_id(self, cur: str, day: int) -> str | None:
        """FX rate row used for ``day``; None for the base currency (rate is 1)."""
        if cur == self.base:
            return None
        d = int(np.clip(day, 0, self.cal.n_days - 1))
        return ids.format_currency_rate_id(cur, self.cal.date(d))

    def to_base_minor(self, amount_minor, cur: str, day):
        """round-half-even(amount * rate * 10^(exp_base - exp_local)), in base minor units."""
        scale = 10.0 ** (self.exp[self.base] - self.exp[cur])
        out = np.rint(np.asarray(amount_minor, dtype=float) * self.rate(cur, day) * scale)
        return out.astype(np.int64)

    def usd_to_local_minor(self, base_major: float, cur: str, day: int) -> int:
        """Amount in ``cur`` minor units worth ``base_major`` base-currency units that day."""
        return max(1, int(round(base_major / float(self.rate(cur, day))
                                * 10.0 ** self.exp[cur])))
