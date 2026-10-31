"""Full generation pipeline, stage by stage in dependency order.

``generate_all`` builds the valid baseline. ``generate_dataset`` additionally injects the
labelled anomalies on their own random stream and returns the ground truth separately.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .anomalies import GROUND_TRUTH_COLUMNS, cases_per_type, inject_anomalies
from .compare import generate_comparisons
from .config import Config
from .forecast import generate_forecasts
from .master import generate_master
from .rng import RngStreams
from .txn import generate_transactions


def generate_all(cfg: Config, reserve_for_anomalies: bool = True) -> dict[str, pd.DataFrame]:
    """The valid baseline: no anomalies.

    The brief asks for ``n_forecast_items`` forecast items in the published data. Anomaly case 3 appends one
    repeated item per case, so by default the clean baseline holds that many fewer, and baseline + repeats
    add up to exactly ``n_forecast_items``. With ``reserve_for_anomalies=False`` (no injection) the clean
    baseline itself has ``n_forecast_items``."""
    streams = RngStreams(cfg.seed)
    tables = generate_master(cfg, streams)
    tables.update(generate_transactions(cfg, tables, streams))
    tables.update(generate_forecasts(cfg, tables, streams,
                                     reserve=cases_per_type(cfg) if reserve_for_anomalies else 0))
    tables.update(generate_comparisons(cfg, tables))
    return tables


@dataclass
class Dataset:
    baseline: dict[str, pd.DataFrame]       # valid data, never modified by the injector
    tables: dict[str, pd.DataFrame]         # what is published as input: baseline + anomalies
    ground_truth: pd.DataFrame              # kept apart from the input tables


def generate_dataset(cfg: Config, inject: bool = True) -> Dataset:
    baseline = generate_all(cfg, reserve_for_anomalies=inject)
    if not inject:
        return Dataset(baseline, {k: v.copy() for k, v in baseline.items()},
                       pd.DataFrame(columns=GROUND_TRUTH_COLUMNS))
    tables, truth = inject_anomalies(cfg, baseline, RngStreams(cfg.seed))
    return Dataset(baseline, tables, truth)
