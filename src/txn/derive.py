"""Values computed *from* the ledger, never drawn: running balances, daily cash
positions per account, and consolidation into the base currency."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .common import Calendar, RateBook


def signed_amount(ledger: pd.DataFrame) -> pd.Series:
    return ledger["amount_minor"].where(ledger["direction"] == "CREDIT", -ledger["amount_minor"])


def running_balances(ledger: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    """Ledger rows in posting order with the account balance after each row."""
    led = ledger.copy()
    led["signed_minor"] = signed_amount(led)
    led = led.sort_values(["bank_account_id", "entry_date", "ledger_entry_id"], kind="stable")
    opening = accounts.set_index("bank_account_id")["opening_balance_minor"]
    led["balance_after_minor"] = (led.groupby("bank_account_id")["signed_minor"].cumsum()
                                  + led["bank_account_id"].map(opening))
    return led


def daily_cash_positions(ledger: pd.DataFrame, accounts: pd.DataFrame, cal: Calendar,
                         rb: RateBook, end_day: int) -> pd.DataFrame:
    """One row per account and calendar day from its opening date to its last live day
    (closure date or end of window): opening + credits - debits = closing, chained day to day."""
    led = ledger.assign(day=ledger["entry_date"].map(cal.offset))
    credit = led["amount_minor"].where(led["direction"] == "CREDIT", 0)
    debit = led["amount_minor"].where(led["direction"] == "DEBIT", 0)
    daily = (pd.DataFrame({"a": led["bank_account_id"], "day": led["day"], "c": credit, "d": debit, "n": 1})
             .groupby(["a", "day"]).sum())
    parts = []
    for acct in accounts.itertuples(index=False):
        first = cal.offset(acct.opening_date)
        last = min(cal.offset(acct.effective_to), end_day) if isinstance(acct.effective_to, str) else end_day
        days = np.arange(first, last + 1)
        cred, deb, cnt = (np.zeros(len(days), dtype=np.int64) for _ in range(3))
        if acct.bank_account_id in daily.index.get_level_values(0):
            sub = daily.loc[acct.bank_account_id]
            pos = sub.index.to_numpy() - first
            if pos.min() < 0 or pos.max() >= len(days):
                raise ValueError(f"ledger entry outside live window of {acct.bank_account_id}")
            cred[pos], deb[pos], cnt[pos] = sub["c"].to_numpy(), sub["d"].to_numpy(), sub["n"].to_numpy()
        closing = acct.opening_balance_minor + np.cumsum(cred - deb)
        opening = np.concatenate(([acct.opening_balance_minor], closing[:-1]))
        parts.append(pd.DataFrame({
            "bank_account_id": acct.bank_account_id,
            "position_date": [cal.iso(d) for d in days], "currency": acct.currency,
            "opening_minor": opening, "total_credits_minor": cred, "total_debits_minor": deb,
            "closing_minor": closing, "entry_count": cnt,
            "fx_rate_id": [rb.rate_id(acct.currency, d) for d in days],
            "closing_base_minor": rb.to_base_minor(closing, acct.currency, days),
        }))
    return pd.concat(parts, ignore_index=True)


def consolidate(positions: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    """Entity-level and group-level closing cash in the base currency, per day.
    Each account is converted with that day's rate (already in closing_base_minor).
    Same arithmetic as the SQL view v_daily_entity_position, plus a group row set."""
    merged = positions.merge(accounts[["bank_account_id", "entity_id"]], on="bank_account_id")
    ent = (merged.groupby(["entity_id", "position_date"], as_index=False)
           .agg(closing_base_minor=("closing_base_minor", "sum"), n_accounts=("bank_account_id", "nunique")))
    grp = (merged.groupby("position_date", as_index=False)
           .agg(closing_base_minor=("closing_base_minor", "sum"), n_accounts=("bank_account_id", "nunique")))
    grp.insert(0, "entity_id", "GROUP")
    return pd.concat([ent, grp], ignore_index=True)
