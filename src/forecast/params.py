"""Parameters of the forecast stage. Each one is explained in docs/distributions.md
(section "Scenarios and forecasts"); tests/test_forecast.py fails if one is missing."""
from __future__ import annotations

# ---- the three scenarios: the ONLY things that differ between them -------------------
# (code, name, receipt_delay_days, collection_haircut_bps, fx_shock_bps, one_off_multiplier_bps)
SCENARIO_DEFINITIONS = (
    ("BASE", "Base case", 0, 0, 0, 10_000),
    ("DELAYED_RECEIPT", "Delayed receipts", 14, 0, 0, 10_000),
    ("STRESS", "Combined stress", 21, 1_000, -1_000, 15_000),
)
SCENARIO_NOTES = {
    "BASE": "Receivables arrive on their expected date, in full. Payables leave on their expected date. "
            "Non-base currencies are valued at the last rate known at the cutoff. No adjustments.",
    "DELAYED_RECEIPT": "Every receivable arrives 14 days later than its expected date (rolled to the next "
                       "business day). Amounts, payables and FX are as in BASE.",
    "STRESS": "Receivables arrive 21 days late and only 90% is collected; every non-base currency is worth "
              "10% less in base currency (applied to flows and to the opening balance); large one-off "
              "payments cost 1.5 times their scheduled amount.",
}

# ---- what a forecast run contains ------------------------------------------------------
FORECAST_MATERIALITY_USD = 75_000.0   # open documents below this are not forecast individually
OVERDUE_MAX_DAYS = 60                 # a receivable/payable overdue longer than this is left out
SCHEDULE_NOTICE_DAYS = 40             # payroll/debt/tax/one-off commitments are known this long before payment
RUN_RATE_LOOKBACK_DAYS = 90           # trailing window of regular flows behind run-rate items
RUN_RATE_MIN_HISTORY_DAYS = 14        # accounts younger than this get no run-rate item
RUN_RATE_BLOCK_DAYS = 10              # one run-rate item per account, direction and block of this length
CUTOFF_DAY_RULE = "last business day of the month"   # documented; implemented in forecast/runs.py

# ---- versions and lifecycle ----------------------------------------------------------------
BASE_RERUN_AT_CUTOFF_INDEX = 1        # the BASE run at this selected cutoff is rejected and re-run (v2)
APPROVED_SHARE = 0.80                 # of runs that complete: approved by review (rest stay COMPLETED)
REVIEW_LAG_DAYS = 1                   # review happens this many calendar days after the run
