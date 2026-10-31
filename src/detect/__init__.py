"""Exception detection and review workflow. Reads dataset tables only, never ground truth."""
from __future__ import annotations

import pandas as pd

from .detectors import DETECTORS, detect_all
from .workflow import ExceptionWorkflow


def run_detection(tables: dict[str, pd.DataFrame]) -> ExceptionWorkflow:
    return ExceptionWorkflow().open_from(detect_all(tables))
