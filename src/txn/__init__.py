"""Transaction stage: receivables, payables, ledger, status history, derived positions."""
from __future__ import annotations

import pandas as pd

from ..config import Config
from ..rng import RngStreams
from ..validate.reconcile import reconcile
from . import params as P
from .common import Calendar, RateBook
from .derive import consolidate, daily_cash_positions
from .items import (Counterparties, build_accounts, generate_trade_items, scheduled_payables)
from .ledger import (build_ledger, fee_entries, item_tables, regular_entries, reversal_entries,
                     settlement_entries, _bd_array)


class ReconciliationError(RuntimeError):
    pass


def generate_transactions(cfg: Config, master: dict[str, pd.DataFrame],
                          streams: RngStreams | None = None) -> dict[str, pd.DataFrame]:
    streams = streams or RngStreams(cfg.seed)
    rng = streams.txn
    cal = Calendar(cfg)
    rb = RateBook(cfg, master["currency_rate"], cal)
    accounts = build_accounts(cfg, cal, master["bank_account"], master["legal_entity"])
    entities = master["legal_entity"]
    cp = Counterparties(rng, list(entities.entity_id))

    sched = scheduled_payables(cfg, rng, cal, rb, accounts, entities)
    target = P.SETTLEMENT_SHARE * cfg.n_ledger
    per_item = 0.9                        # settlement entries per trade item, refined each round
    n_trade = max(10, int((target - len(sched)) / per_item))
    for round_no in range(P.CALIBRATION_ROUNDS):
        n_r = round(n_trade * P.RECEIVABLE_SHARE)
        trade = generate_trade_items(cfg, rng, cal, rb, accounts, cp, n_r, n_trade - n_r)
        items = sched + trade
        entries = settlement_entries(rng, items)
        n_set = len(entries)
        if abs(n_set - target) <= P.SETTLEMENT_TOLERANCE * target or round_no == P.CALIBRATION_ROUNDS - 1:
            break
        per_item = max(0.2, (n_set - len(sched)) / max(1, len(trade)))
        n_trade = max(10, int((target - len(sched)) / per_item))
    if n_set > P.MAX_SETTLEMENT_FRACTION * cfg.n_ledger:
        raise ValueError("settlements exceed the share of the ledger available; raise n_ledger")

    bds = _bd_array(cal, cfg.n_days)
    n_fee = round(P.FEE_SHARE * cfg.n_ledger)
    n_rev = round(P.REVERSAL_PAIR_SHARE * cfg.n_ledger)
    n_reg = cfg.n_ledger - n_set - n_fee - n_rev
    if n_reg <= 0:
        raise ValueError("n_ledger too small for the settlements generated")
    fees = fee_entries(rng, cal, rb, accounts, n_fee, bds)
    regular = regular_entries(rng, cal, rb, accounts, n_reg, entries + fees, bds)
    reversals = reversal_entries(rng, cal, regular, n_rev)
    all_entries = entries + fees + regular + reversals
    ledger = build_ledger(cal, rb, all_entries)
    rcv, pay, links, rev_ev, pay_ev = item_tables(cal, items, all_entries)

    positions = daily_cash_positions(ledger, master["bank_account"], cal, rb, cfg.n_days - 1)
    breaks = reconcile(ledger, positions, master["bank_account"])
    if len(breaks):
        raise ReconciliationError(f"{len(breaks)} reconciliation breaks, first: {breaks.iloc[0].to_dict()}")
    return {"receivable": rcv, "payable": pay, "ledger_entry": ledger, "settlement_link": links,
            "receivable_status_event": rev_ev, "payable_status_event": pay_ev,
            "daily_cash_position": positions}

