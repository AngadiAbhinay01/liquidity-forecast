"""Exception-handling parameters (explained in docs/distributions.md, section "Exception handling")."""
from __future__ import annotations

SEVERITY_ORDER = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
BASE_SEVERITY = {
    "OPENING_PLUS_LEDGER_NE_CLOSING": "HIGH",
    "FX_RATE_MISSING": "MEDIUM",
    "DOUBLE_COUNTED_CASH_FLOW": "HIGH",
    "FORECAST_USES_ACTUAL_AFTER_CUTOFF": "CRITICAL",
    "CURRENCY_MIXED_WITHOUT_CONVERSION": "HIGH",
}
SEVERITY_BUMP_USD = 100_000.0        # an exception at least this large (base currency) is raised one level, up to HIGH
SEVERITY_BUMP_CAP = "HIGH"           # a size bump never makes an exception CRITICAL

# playbook: recommended decision per issue type, and who handles it
RECOMMENDED_DECISION = {
    "OPENING_PLUS_LEDGER_NE_CLOSING": "CORRECT_POSITION_FROM_LEDGER",
    "FX_RATE_MISSING": "REQUEST_FX_RATE_AND_REVALUE",
    "DOUBLE_COUNTED_CASH_FLOW": "REMOVE_DUPLICATE_ITEM",
    "FORECAST_USES_ACTUAL_AFTER_CUTOFF": "REJECT_ITEM_AND_ESCALATE",
    "CURRENCY_MIXED_WITHOUT_CONVERSION": "CORRECT_CURRENCY_AND_REVALUE",
}
RISK_COMPLIANCE_TYPES = ("OPENING_PLUS_LEDGER_NE_CLOSING", "CURRENCY_MIXED_WITHOUT_CONVERSION")
DISMISS_DECISION = "DISMISS_AS_FALSE_POSITIVE"      # only the risk/compliance reviewer may use it
