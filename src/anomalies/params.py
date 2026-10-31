"""Anomaly injection parameters. Explained in docs/distributions.md (section "Anomalies")."""
from __future__ import annotations

ANOMALIES_PER_TYPE_FULL = 10         # cases of each type at full scale; scales with n_ledger
ANOMALIES_PER_TYPE_MIN = 3           # never fewer than this, so every type exists at starter scale
MIN_POSITION_DAYS = 20               # an account needs this many position rows to be a target
CLOSING_ERROR_FRACTION = (0.01, 0.20)    # |closing| * U(...) + CLOSING_ERROR_FLOOR_MINOR, random sign
CLOSING_ERROR_FLOOR_MINOR = 100

ISSUE_TYPES = ("OPENING_PLUS_LEDGER_NE_CLOSING", "FX_RATE_MISSING", "DOUBLE_COUNTED_CASH_FLOW",
               "FORECAST_USES_ACTUAL_AFTER_CUTOFF", "CURRENCY_MIXED_WITHOUT_CONVERSION")

# issue_type -> (table, expected decision, expected reviewer role)
EXPECTED = {
    "OPENING_PLUS_LEDGER_NE_CLOSING": ("daily_cash_position", "CORRECT_POSITION_FROM_LEDGER",
                                       "RISK_COMPLIANCE_REVIEWER"),
    "FX_RATE_MISSING": ("ledger_entry", "REQUEST_FX_RATE_AND_REVALUE", "OPERATIONS_ANALYST"),
    "DOUBLE_COUNTED_CASH_FLOW": ("cash_flow_forecast", "REMOVE_DUPLICATE_ITEM", "OPERATIONS_ANALYST"),
    "FORECAST_USES_ACTUAL_AFTER_CUTOFF": ("cash_flow_forecast", "REJECT_ITEM_AND_ESCALATE",
                                          "RISK_COMPLIANCE_REVIEWER"),
    "CURRENCY_MIXED_WITHOUT_CONVERSION": ("ledger_entry", "CORRECT_CURRENCY_AND_REVALUE",
                                          "RISK_COMPLIANCE_REVIEWER"),
}
