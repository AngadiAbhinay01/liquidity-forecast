"""Parameters of the transaction stage (receivables, payables, ledger).

Every constant is explained in docs/distributions.md (section "Transactions");
tests/test_transactions.py fails if one is missing there.
"""
from __future__ import annotations

# ---- ledger composition ------------------------------------------------------
SETTLEMENT_SHARE = 0.45          # target share of ledger rows that settle a receivable/payable
SETTLEMENT_TOLERANCE = 0.10      # accepted relative miss of that target before recalibrating
CALIBRATION_ROUNDS = 4
MAX_SETTLEMENT_FRACTION = 0.80   # above this there is not enough room for other flows: error
RECEIVABLE_SHARE = 0.55          # of settlement entries that are receipts (rest: payments)
FEE_SHARE = 0.04                 # FEE_OR_INTEREST rows as a share of n_ledger
FEE_DEBIT_PROB = 0.75            # fee (debit) vs interest income (credit)
FEE_MEDIAN_USD = 40.0
FEE_SIGMA = 0.8
REVERSAL_PAIR_SHARE = 0.003      # reversal rows as a share of n_ledger
REVERSAL_MAX_GAP_DAYS = 5
DORMANT_ACTIVITY_FACTOR = 0.10   # weight of a DORMANT account for regular flows
MIN_AMOUNT_USD = 50.0            # smallest receivable/payable/regular flow (keeps base amount > 0)

# ---- receivables -------------------------------------------------------------
PRE_WINDOW_DAYS = 45             # invoices may pre-date the window so January has open items
CUSTOMERS_PER_ENTITY = 12
RISK_TIER_WEIGHTS = {"LOW": 0.50, "MEDIUM": 0.35, "HIGH": 0.15}
TERMS_DAYS = (15, 30, 45, 60)
TERMS_WEIGHTS = (0.15, 0.45, 0.25, 0.15)
RECEIVABLE_MEDIAN_USD = 25_000.0
RECEIVABLE_SIGMA = 1.0
LATE_PROB = {"LOW": 0.10, "MEDIUM": 0.30, "HIGH": 0.60}
LATE_MEAN_DAYS = {"LOW": 4.0, "MEDIUM": 9.0, "HIGH": 18.0}
LATE_MAX_DAYS = 75
EARLY_PROB = 0.12
EARLY_MAX_DAYS = 5
PARTIAL_PROB = {"LOW": 0.08, "MEDIUM": 0.12, "HIGH": 0.20}
PARTIAL_FIRST_FRACTION = (0.40, 0.80)
PARTIAL_GAP_MEAN_DAYS = 12.0
PARTIAL_THIRD_TRANCHE_PROB = 0.30
PARTIAL_SECOND_FRACTION = (0.30, 0.70)
WRITE_OFF_PROB = {"LOW": 0.0, "MEDIUM": 0.01, "HIGH": 0.05}
PARTIAL_WRITE_OFF_PROB_HIGH = 0.15   # a HIGH-tier partial payer never pays the rest
WRITE_OFF_AFTER_DUE_DAYS = 90
CANCEL_PROB = 0.02
CANCEL_WITHIN_DAYS = (1, 10)

# ---- trade payables ----------------------------------------------------------
SUPPLIERS_PER_ENTITY = 12
PAYABLE_MEDIAN_USD = 15_000.0
PAYABLE_SIGMA = 1.0
PAYABLE_TIMING_WEIGHTS = (0.85, 0.10, 0.05)     # on time, early, late
PAYABLE_EARLY_MAX_DAYS = 5
PAYABLE_LATE_MEAN_DAYS = 7.0
PAYABLE_PARTIAL_PROB = 0.05
PAYABLE_CANCEL_PROB = 0.02

# ---- scheduled and one-off payables -----------------------------------------
PAYROLL_MEDIAN_USD = 60_000.0
PAYROLL_SIGMA = 0.4
PAYROLL_MONTH_NOISE = 0.02
PAYROLL_PAY_DAY = 25             # last weekday on or before this day of the month
DEBT_SERVICE_ENTITY_SHARE = 0.40
DEBT_SERVICE_MEDIAN_USD = 80_000.0
DEBT_SERVICE_SIGMA = 0.5
DEBT_SERVICE_DAY = 5
TAX_MONTHS = (1, 4, 7, 10)
TAX_DAY = 15
TAX_MEDIAN_USD = 120_000.0
TAX_SIGMA = 0.5
ONE_OFF_PER_ENTITY = 6.0         # one-off count = max(ONE_OFF_MIN_COUNT, round(this * n_entities))
ONE_OFF_MIN_COUNT = 3
ONE_OFF_MEDIAN_USD = 350_000.0
ONE_OFF_SIGMA = 0.5
ONE_OFF_CAPEX_SHARE = 0.70
ONE_OFF_TERMS_DAYS = 14

# ---- batching (one bank entry settling several documents) ---------------------
BATCH_PROB = 0.35
BATCH_WINDOW_DAYS = 4
BATCH_MAX_ITEMS = 5

# ---- regular operating flows --------------------------------------------------
BALANCE_TARGET_FLOOR_USD = 30_000.0
FEEDBACK_SCALE = 0.5             # sigmoid width as a fraction of the target balance
REGULAR_MEDIAN_USD = {"OPERATING": 9_000.0, "COLLECTION": 12_000.0, "DISBURSEMENT": 8_000.0,
                      "PAYROLL": 4_000.0, "RESERVE": 5_000.0}
REGULAR_SIGMA = 0.9
REGULAR_MAX_CORRECTION = 4.0     # corrective flows are scaled by at most this factor
