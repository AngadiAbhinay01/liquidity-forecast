"""BankAccount generation. Distributions are listed in docs/distributions.md.

Needs the entity table and the FX table: opening balances are drawn in USD terms
and converted to the account currency at the rate on its opening date.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from ..config import Config
from ..ids import IdFactory
from . import params as P

COLUMNS = ["bank_account_id", "entity_id", "account_seq", "bank_name", "account_type", "currency",
           "opening_balance_minor", "opening_date", "status", "effective_from", "effective_to"]


def generate_bank_accounts(cfg: Config, rng: np.random.Generator, ids: IdFactory,
                           entities: pd.DataFrame, rates_wide: pd.DataFrame) -> pd.DataFrame:
    n_ent, n_acc = cfg.n_entities, cfg.n_accounts
    exponent = {c.code: c.minor_unit_exponent for c in cfg.currencies}
    codes = list(cfg.currency_codes)

    # ---- how many accounts each entity gets: 1 each, the rest multinomial
    weights = entities["entity_type"].map(P.EXTRA_ACCOUNT_WEIGHT).to_numpy(dtype=float)
    counts = 1 + rng.multinomial(n_acc - n_ent, weights / weights.sum())

    entity_rows = entities.reset_index(drop=True)
    flat: list[tuple[int, int]] = [(e, s) for e in range(n_ent) for s in range(1, int(counts[e]) + 1)]
    assert len(flat) == n_acc

    # ---- all random draws up front, one array per attribute, so the draw count
    #      does not depend on which branch each account takes
    type_u = rng.random(n_acc)
    foreign_u = rng.random(n_acc)
    ccy_u = rng.random(n_acc)
    bank_i = rng.integers(0, len(P.BANKS), n_acc)
    existing_u = rng.random(n_acc)
    age_days = rng.integers(P.EXISTING_ACCOUNT_AGE_DAYS[0], P.EXISTING_ACCOUNT_AGE_DAYS[1] + 1, n_acc)
    open_frac = rng.uniform(P.NEW_ACCOUNT_OPEN_FRACTION[0], P.NEW_ACCOUNT_OPEN_FRACTION[1], n_acc)
    zero_u = rng.random(n_acc)
    balance_z = rng.standard_normal(n_acc)
    closure_frac = rng.uniform(P.CLOSURE_WINDOW_FRACTION[0], P.CLOSURE_WINDOW_FRACTION[1], n_acc)

    type_names = list(P.ACCOUNT_TYPE_WEIGHTS)
    type_cum = np.cumsum(np.array(list(P.ACCOUNT_TYPE_WEIGHTS.values())) / sum(P.ACCOUNT_TYPE_WEIGHTS.values()))

    recs = []
    for j, (e, seq) in enumerate(flat):
        ent = entity_rows.iloc[e]
        functional = ent["functional_currency"]
        primary = seq == 1
        if primary:
            acct_type, currency = "OPERATING", functional
        else:
            acct_type = type_names[min(int(np.searchsorted(type_cum, type_u[j], side="right")), len(type_names) - 1)]
            others = [c for c in codes if c != functional]
            if others and foreign_u[j] < P.FOREIGN_CURRENCY_SHARE:
                currency = others[min(int(ccy_u[j] * len(others)), len(others) - 1)]
            else:
                currency = functional

        entity_from = date.fromisoformat(ent["effective_from"])
        is_existing = primary or existing_u[j] < P.EXISTING_ACCOUNT_SHARE
        if is_existing:
            opening = cfg.date_start
            eff_from = max(entity_from, cfg.date_start - timedelta(days=int(age_days[j])))
        else:
            opening = cfg.date_start + timedelta(days=int(open_frac[j] * (cfg.n_days - 1)))
            eff_from = opening
        recs.append(dict(e=e, seq=seq, primary=primary, acct_type=acct_type, currency=currency,
                         opening=opening, eff_from=eff_from, existing=is_existing, j=j))

    # ---- status: accounts of a closed entity close with it; quotas for the rest
    status = ["ACTIVE"] * n_acc
    eff_to: list = [None] * n_acc
    for r in recs:
        ent = entity_rows.iloc[r["e"]]
        if ent["status"] == "CLOSED":
            status[r["j"]] = "CLOSED"
            eff_to[r["j"]] = date.fromisoformat(ent["effective_to"])
    eligible = [r["j"] for r in recs if not r["primary"] and status[r["j"]] == "ACTIVE"]
    if n_acc >= P.MIN_ACCOUNTS_FOR_STATUS_QUOTAS and eligible:
        k_dormant = min(len(eligible), max(1, round(P.DORMANT_ACCOUNT_SHARE * n_acc)))
        k_closed = min(len(eligible) - k_dormant, max(1, round(P.CLOSED_ACCOUNT_SHARE * n_acc)))
        picks = [eligible[int(i)] for i in rng.permutation(len(eligible))]
        for j in picks[:k_dormant]:
            status[j] = "DORMANT"
        for j in picks[k_dormant:k_dormant + k_closed]:
            status[j] = "CLOSED"
            eff_to[j] = cfg.date_start + timedelta(days=int(closure_frac[j] * (cfg.n_days - 1)))

    # ---- opening balance: lognormal in USD terms, converted at the opening-date rate
    rows = []
    for r in recs:
        j = r["j"]
        usd = float(np.exp(np.log(P.OPENING_BALANCE_MEDIAN_USD[r["acct_type"]]) + P.OPENING_BALANCE_SIGMA * balance_z[j]))
        if not r["existing"]:
            usd *= P.NEW_ACCOUNT_BALANCE_FACTOR
        if zero_u[j] < P.ZERO_BALANCE_SHARE:
            usd = 0.0
        rate = float(rates_wide.at[r["opening"].isoformat(), r["currency"]])
        minor = int(np.rint(usd / rate * 10 ** exponent[r["currency"]]))
        entity_id = entity_rows.iloc[r["e"]]["entity_id"]
        rows.append({
            "bank_account_id": ids.bank_account(entity_id),
            "entity_id": entity_id,
            "account_seq": r["seq"],
            "bank_name": P.BANKS[int(bank_i[j])],
            "account_type": r["acct_type"],
            "currency": r["currency"],
            "opening_balance_minor": minor,
            "opening_date": r["opening"].isoformat(),
            "status": status[j],
            "effective_from": r["eff_from"].isoformat(),
            "effective_to": None if eff_to[j] is None else eff_to[j].isoformat(),
        })
    return pd.DataFrame(rows, columns=COLUMNS)
