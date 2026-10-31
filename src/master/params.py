"""Every tunable number behind the master data, in one place.

docs/distributions.md explains each of these and tests/test_master.py fails if a
constant here is not mentioned there. The levels below are illustrative
synthetic values, NOT market data: no external dataset is used (lawful-use
requirement), so there is nothing to attribute or license.
"""
from __future__ import annotations

# ============================================================== legal entities
# country -> (functional currency, sampling weight). Countries whose currency is
# not in the config are dropped and the weights renormalised.
COUNTRIES: dict[str, tuple[str, float]] = {
    "US": ("USD", 0.22),
    "GB": ("GBP", 0.14),
    "DE": ("EUR", 0.14),
    "FR": ("EUR", 0.06),
    "NL": ("EUR", 0.04),
    "IN": ("INR", 0.28),
    "JP": ("JPY", 0.12),
}
# The first entities are stratified so every configured currency is somebody's
# functional currency; the rest are drawn from COUNTRIES by weight.

TREASURY_CENTRE_SHARE = 0.10          # of entities (at least 1 once n_entities >= 3)
ENTITY_AGE_DAYS = (365, 3650)         # uniform integer: years before date_start the entity began
SUSPENDED_ENTITY_SHARE = 0.08         # of entities, at least 1 once n_entities >= 6
CLOSED_ENTITY_SHARE = 0.05            # of entities, at least 1 once n_entities >= 6
MIN_ENTITIES_FOR_STATUS_QUOTAS = 6
CLOSURE_WINDOW_FRACTION = (0.75, 0.97)  # closure falls in the last quarter of the calendar
SUSPENSION_WINDOW_FRACTION = (0.30, 0.70)  # suspension starts in the middle of the calendar: some activity before, none after

BUFFER_MEDIAN_USD = 250_000           # lognormal median of min liquidity buffer
BUFFER_SIGMA = 0.7                    # lognormal sigma (of ln)
BUFFER_MULTIPLIER = {"HOLDING": 2.0, "TREASURY_CENTRE": 2.0, "OPERATING": 1.0}

NAME_SYLLABLES = ("ka", "vel", "or", "tri", "mar", "den", "sil", "vor", "lum", "bex",
                  "tan", "ora", "nev", "quo", "ris", "fen", "dal", "mer", "zin", "pol")
NAME_STEM_SYLLABLES = (2, 3)          # uniform: number of syllables in the invented stem
INDUSTRY_WORDS = ("Logistics", "Foods", "Industries", "Systems", "Textiles", "Energy",
                  "Components", "Trading", "Labs", "Packaging")
LEGAL_SUFFIX = {"US": "Inc", "GB": "Ltd", "DE": "GmbH", "FR": "SAS", "NL": "B.V.",
                "IN": "Pvt Ltd", "JP": "K.K."}

# ============================================================== bank accounts
EXTRA_ACCOUNT_WEIGHT = {"HOLDING": 1.0, "TREASURY_CENTRE": 1.5, "OPERATING": 1.0}
# Every entity gets 1 primary OPERATING account in its functional currency; the
# remaining (n_accounts - n_entities) are spread by multinomial with these weights.

ACCOUNT_TYPE_WEIGHTS = {"OPERATING": 0.30, "COLLECTION": 0.25, "DISBURSEMENT": 0.20,
                        "PAYROLL": 0.15, "RESERVE": 0.10}   # non-primary accounts
FOREIGN_CURRENCY_SHARE = 0.35         # P(non-primary account is NOT in the functional currency)
BANKS = ("Northgate Bank", "Meridian Trust", "Alder & Finch Bank", "Corvane Bank",
         "Halcyon Commercial Bank", "Brightwater Bank", "Tessel Savings", "Ostrava Merchant Bank",
         "Pennant Bank", "Lowell & Rye", "Stonebridge Bank", "Calder Commercial")

EXISTING_ACCOUNT_SHARE = 0.85         # accounts already open at date_start
EXISTING_ACCOUNT_AGE_DAYS = (30, 1500)  # uniform: how long before date_start they became effective
NEW_ACCOUNT_OPEN_FRACTION = (0.02, 0.50)  # uniform: where in the calendar a new account opens

DORMANT_ACCOUNT_SHARE = 0.06          # of accounts, at least 1 once n_accounts >= 15
CLOSED_ACCOUNT_SHARE = 0.04           # of accounts, at least 1 once n_accounts >= 15
MIN_ACCOUNTS_FOR_STATUS_QUOTAS = 15

OPENING_BALANCE_MEDIAN_USD = {"OPERATING": 400_000, "COLLECTION": 250_000, "DISBURSEMENT": 150_000,
                              "PAYROLL": 100_000, "RESERVE": 800_000}
OPENING_BALANCE_SIGMA = 0.9           # lognormal sigma (of ln), same for every type
NEW_ACCOUNT_BALANCE_FACTOR = 0.10     # newly opened accounts start with 10% of a mature balance
ZERO_BALANCE_SHARE = 0.03             # boundary case: opening balance exactly 0

# ============================================================== FX rates
# Quote = base units per 1 unit of the quote currency (USD per EUR, ...).
INITIAL_RATE = {"EUR": 1.08, "GBP": 1.27, "INR": 0.0120, "JPY": 0.0067}
DAILY_VOL = {"EUR": 0.0040, "GBP": 0.0045, "INR": 0.0020, "JPY": 0.0055}  # sigma of daily ln return, weekdays
DAILY_DRIFT = {"EUR": 0.0, "GBP": 0.0, "INR": -0.0001, "JPY": 0.0}        # ln return per weekday
MEAN_REVERSION = 0.005                # pull per weekday back toward the drift line (keeps levels plausible)

SHOCK_EVERY_DAYS = 120               # market-wide shock dates = max(1, round(n_days / 120)); 3 in a 365-day year
SHOCK_MIN_SPACING_DAYS = 20
SHOCK_EDGE_DAYS = 10                  # no shock in the first/last 10 calendar days
SHOCK_SIZE = (0.02, 0.04)             # uniform |ln move| before beta
SHOCK_BETA = {"EUR": 1.0, "GBP": 1.2, "INR": 0.8, "JPY": 0.8}
SHOCK_DIRECTION = {"EUR": -1, "GBP": -1, "INR": -1, "JPY": +1}  # risk-off: USD up, yen up
POST_SHOCK_VOL_BOOST = 2.0            # extra vol multiplier the day after a shock ...
POST_SHOCK_DECAY_DAYS = 3.0           # ... decaying as exp(-k / 3)
FX_DECIMALS = 8                       # rates are rounded to 8 dp before they are stored
