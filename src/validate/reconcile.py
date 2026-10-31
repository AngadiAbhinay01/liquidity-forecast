"""Reconciliation: opening + sum(ledger) == closing for every account-day.

Computed a different way from ``txn.derive``: derive accumulates by grouping and
cumulative sums; this check looks up, for each stored position, the sum of all ledger
entries up to that date with a sorted-array search, starting from the account's
opening balance in the master table. Returns one row per break (empty = clean).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BREAK_COLUMNS = ["check", "bank_account_id", "position_date", "expected", "actual", "diff"]


def _breaks(check: str, frame: pd.DataFrame, expected, actual) -> pd.DataFrame:
    bad = np.asarray(expected) != np.asarray(actual)
    out = pd.DataFrame({"check": check, "bank_account_id": frame["bank_account_id"].to_numpy()[bad],
                        "position_date": frame["position_date"].to_numpy()[bad],
                        "expected": np.asarray(expected)[bad], "actual": np.asarray(actual)[bad]})
    out["diff"] = out["actual"] - out["expected"]
    return out[BREAK_COLUMNS]


def reconcile(ledger: pd.DataFrame, positions: pd.DataFrame, accounts: pd.DataFrame) -> pd.DataFrame:
    parts = []
    # 1. arithmetic of each stored row
    parts.append(_breaks("ARITHMETIC", positions,
                         positions.opening_minor + positions.total_credits_minor - positions.total_debits_minor,
                         positions.closing_minor))
    # 2. chain: opening = previous closing; first day = account opening balance
    pos = positions.sort_values(["bank_account_id", "position_date"], kind="stable").reset_index(drop=True)
    prev = pos.groupby("bank_account_id")["closing_minor"].shift(1)
    first = prev.isna()
    master_open = pos["bank_account_id"].map(accounts.set_index("bank_account_id")["opening_balance_minor"])
    expected_open = prev.where(~first, master_open)
    parts.append(_breaks("CHAIN", pos, expected_open.astype("int64"), pos.opening_minor))
    # 3. independent: master opening balance + ledger entries up to the day == closing
    sign = np.where(ledger["direction"] == "CREDIT", 1, -1) * ledger["amount_minor"].to_numpy()
    led = pd.DataFrame({"a": ledger["bank_account_id"], "d": ledger["entry_date"], "s": sign,
                        "c": np.where(sign > 0, sign, 0), "x": np.where(sign < 0, -sign, 0)})
    led = led.sort_values(["a", "d"], kind="stable")
    exp_close = np.zeros(len(pos), dtype=np.int64)
    exp_cred = np.zeros(len(pos), dtype=np.int64)
    exp_deb = np.zeros(len(pos), dtype=np.int64)
    exp_cnt = np.zeros(len(pos), dtype=np.int64)
    opening = accounts.set_index("bank_account_id")["opening_balance_minor"]
    groups = {k: g for k, g in led.groupby("a")}
    for acct, idx in pos.groupby("bank_account_id").indices.items():
        g = groups.get(acct)
        dates = pos["position_date"].to_numpy()[idx]
        if g is None:
            exp_close[idx] = opening[acct]
            continue
        gd = g["d"].to_numpy()
        cs, cc, cx = np.cumsum(g["s"].to_numpy()), np.cumsum(g["c"].to_numpy()), np.cumsum(g["x"].to_numpy())
        hi = np.searchsorted(gd, dates, side="right")      # entries on or before the day
        lo = np.searchsorted(gd, dates, side="left")       # entries strictly before the day
        pad = lambda a: np.concatenate(([0], a))
        exp_close[idx] = opening[acct] + pad(cs)[hi]
        exp_cred[idx], exp_deb[idx] = pad(cc)[hi] - pad(cc)[lo], pad(cx)[hi] - pad(cx)[lo]
        exp_cnt[idx] = hi - lo
    parts.append(_breaks("LEDGER_CUMULATIVE", pos, exp_close, pos.closing_minor))
    parts.append(_breaks("CREDITS_VS_LEDGER", pos, exp_cred, pos.total_credits_minor))
    parts.append(_breaks("DEBITS_VS_LEDGER", pos, exp_deb, pos.total_debits_minor))
    parts.append(_breaks("COUNT_VS_LEDGER", pos, exp_cnt, pos.entry_count))
    # 4. every ledger row lands on a position row
    keys = set(zip(pos.bank_account_id, pos.position_date))
    missing = [(a, d) for a, d in zip(ledger.bank_account_id, ledger.entry_date) if (a, d) not in keys]
    if missing:
        parts.append(pd.DataFrame({"check": "LEDGER_OFF_GRID", "bank_account_id": [m[0] for m in missing],
                                   "position_date": [m[1] for m in missing],
                                   "expected": 0, "actual": 1, "diff": 1})[BREAK_COLUMNS])
    return pd.concat(parts, ignore_index=True)
