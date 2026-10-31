"""Generation parameters.

Every number that shapes the dataset lives here, so the starter and full
datasets come from the same generator with different parameters. Nothing in this
module reads the clock, the environment or any random source.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from typing import Any

DEFAULT_SEED = 20261022


@dataclass(frozen=True)
class Currency:
    code: str
    name: str
    minor_unit_exponent: int
    is_base: bool = False


# Matches the currency lookup table in sql/schema.sql. USD is the base
# (reporting) currency; see docs/design_decisions.md item 18.
CURRENCIES: tuple[Currency, ...] = (
    Currency("USD", "US Dollar", 2, is_base=True),
    Currency("EUR", "Euro", 2),
    Currency("GBP", "Pound Sterling", 2),
    Currency("INR", "Indian Rupee", 2),
    Currency("JPY", "Japanese Yen", 0),
)


@dataclass(frozen=True)
class ScaleParams:
    n_entities: int
    n_accounts: int
    n_ledger: int
    n_forecast_items: int


SCALES: dict[str, ScaleParams] = {
    # The sizes required by the brief.
    "full": ScaleParams(n_entities=30, n_accounts=150, n_ledger=100_000, n_forecast_items=20_000),
    # Same generator, small enough to read by eye and to commit to the repo.
    "starter": ScaleParams(n_entities=6, n_accounts=18, n_ledger=3_000, n_forecast_items=600),
}


@dataclass(frozen=True)
class Config:
    seed: int
    scale_name: str
    n_entities: int
    n_accounts: int
    n_ledger: int
    n_forecast_items: int
    currencies: tuple[Currency, ...] = CURRENCIES
    # Simulated business calendar. Positions, ledger and rates cover
    # date_start..date_end inclusive.
    date_start: date = date(2026, 1, 1)
    date_end: date = date(2026, 12, 31)
    # Days of future cash flows each forecast run projects.
    forecast_horizon_days: int = 30

    # ------------------------------------------------------------------ derived
    @property
    def base_currency(self) -> Currency:
        return next(c for c in self.currencies if c.is_base)

    @property
    def currency_codes(self) -> tuple[str, ...]:
        return tuple(c.code for c in self.currencies)

    @property
    def n_days(self) -> int:
        return (self.date_end - self.date_start).days + 1

    @property
    def last_cutoff_date(self) -> date:
        """Latest cutoff whose full horizon still has actuals inside the calendar."""
        return self.date_end - timedelta(days=self.forecast_horizon_days)

    # --------------------------------------------------------------- validation
    def validate(self) -> "Config":
        problems: list[str] = []
        if not isinstance(self.seed, int) or isinstance(self.seed, bool) or not 0 <= self.seed < 2**63:
            problems.append(f"seed must be an integer in [0, 2**63), got {self.seed!r}")
        for name in ("n_entities", "n_accounts", "n_ledger", "n_forecast_items"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                problems.append(f"{name} must be a positive integer, got {value!r}")
        if not problems and self.n_accounts < self.n_entities:
            problems.append("n_accounts must be >= n_entities (every entity needs an account)")
        if not problems and self.n_accounts > self.n_entities * 999:
            problems.append("n_accounts allows at most 999 accounts per entity")
        if self.date_end <= self.date_start:
            problems.append("date_end must be after date_start")
        if self.forecast_horizon_days < 1:
            problems.append("forecast_horizon_days must be >= 1")
        elif self.last_cutoff_date <= self.date_start:
            problems.append("date range is too short for the forecast horizon")
        codes = self.currency_codes
        if len(set(codes)) != len(codes):
            problems.append("duplicate currency codes")
        if sum(c.is_base for c in self.currencies) != 1:
            problems.append("exactly one currency must be the base currency")
        if problems:
            raise ValueError("invalid config: " + "; ".join(problems))
        return self

    # ------------------------------------------------------------ serialisation
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["date_start"] = self.date_start.isoformat()
        d["date_end"] = self.date_end.isoformat()
        d["currencies"] = [asdict(c) for c in self.currencies]
        return d

    def fingerprint(self) -> str:
        """SHA-256 of the canonical JSON form: same parameters, same fingerprint."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_config(scale: str = "full", seed: int = DEFAULT_SEED, **overrides: Any) -> Config:
    """Build a validated Config from a named scale, with optional per-field overrides."""
    if scale not in SCALES:
        raise ValueError(f"unknown scale {scale!r}; choose from {sorted(SCALES)}")
    p = SCALES[scale]
    cfg = Config(
        seed=seed,
        scale_name=scale,
        n_entities=p.n_entities,
        n_accounts=p.n_accounts,
        n_ledger=p.n_ledger,
        n_forecast_items=p.n_forecast_items,
    )
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if overrides:
        cfg = replace(cfg, **overrides)
    return cfg.validate()
