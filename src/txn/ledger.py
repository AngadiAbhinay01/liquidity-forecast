"""LedgerEntry generation and the receivable/payable/event tables.

Order of work:
  1. settlement tranches of items are batched (N documents -> 1 bank entry) or kept 1:1;
  2. fees/interest are scattered over accounts;
  3. regular operating flows are drawn per account in date order. Their direction and
     size react to the running balance, so accounts do not drift without limit. This is
     the only place where a ledger amount depends on a balance; the balances that are
     *reported* (daily_cash_position) are always recomputed from the finished ledger;
  4. reversals duplicate a few regular entries with opposite direction;
  5. entries are sorted, numbered per year and converted to base currency.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .. import ids
from . import params as P
from .common import Calendar, RateBook
from .events import item_events
from .items import Acct, Item


@dataclass
class Entry:
    seq: int
    account: Acct
    day: int
    direction: str            # CREDIT / DEBIT
    amount: int
    etype: str
    narrative: str
    allocs: list = field(default_factory=list)      # [(item, tranche_idx, amount)]
    reverses_seq: int | None = None

    @property
    def signed(self) -> int:
        return self.amount if self.direction == "CREDIT" else -self.amount


def settlement_entries(rng, items: list[Item]) -> list[Entry]:
    """Turn tranches into ledger entries; batch some single-tranche items."""
    events = []     # (account_id, cparty, kind, day, order, item, tranche_idx)
    for order, it in enumerate(items):
        for ti, (d, a) in enumerate(it.tranches):
            events.append((it.account.account_id, it.cparty, it.kind, d, order, it, ti))
    events.sort(key=lambda e: e[:5])
    entries: list[Entry] = []
    i = 0
    while i < len(events):
        e = events[i]
        group = [e]
        if e[5].batchable:
            j = i + 1
            while (j < len(events) and len(group) < P.BATCH_MAX_ITEMS
                   and events[j][5].batchable and events[j][:3] == e[:3]
                   and events[j][3] - e[3] <= P.BATCH_WINDOW_DAYS
                   and all(events[j][5] is not g[5] for g in group)
                   and rng.random() < P.BATCH_PROB):
                group.append(events[j])
                j += 1
            i = j if len(group) > 1 else i + 1
        else:
            i += 1
        day = max(g[3] for g in group)
        it0 = group[0][5]
        direction = "CREDIT" if it0.kind == "R" else "DEBIT"
        etype = "RECEIPT_SETTLEMENT" if it0.kind == "R" else "PAYMENT_SETTLEMENT"
        total = 0
        allocs = []
        for g in group:
            amt = g[5].tranches[g[6]][1]
            g[5].tranches[g[6]][0] = day            # batched documents settle on the batch date
            allocs.append((g[5], g[6], amt))
            total += amt
        if len(group) > 1:
            text = f"{'Remittance from' if it0.kind == 'R' else 'Batch payment to'} {it0.cparty} ({len(group)} documents)"
        else:
            text = f"{'Receipt from' if it0.kind == 'R' else 'Payment to'} {it0.cparty}"
        entries.append(Entry(-1, it0.account, day, direction, total, etype, text, allocs))
    return entries


def _bd_array(cal: Calendar, n_days: int) -> np.ndarray:
    return np.array([d for d in range(n_days) if cal.is_bd(d)])


def _spread(rng, total: int, accounts: list[Acct]) -> np.ndarray:
    w = np.array([a.weight for a in accounts], dtype=float)
    return rng.multinomial(total, w / w.sum())


def fee_entries(rng, cal, rb, accounts, n_fee: int, bds: np.ndarray) -> list[Entry]:
    out = []
    for a, k in zip(accounts, _spread(rng, n_fee, accounts)):
        lo, hi = np.searchsorted(bds, a.first_bd), np.searchsorted(bds, a.last_bd, side="right")
        for _ in range(int(k)):
            day = int(bds[rng.integers(lo, hi)])
            usd = max(P.MIN_AMOUNT_USD, P.FEE_MEDIAN_USD * float(np.exp(P.FEE_SIGMA * rng.standard_normal())))
            debit = rng.random() < P.FEE_DEBIT_PROB
            out.append(Entry(-1, a, day, "DEBIT" if debit else "CREDIT",
                             rb.usd_to_local_minor(usd, a.currency, day), "FEE_OR_INTEREST",
                             "Bank charges" if debit else "Interest credited"))
    return out


def regular_entries(rng, cal, rb, accounts, n_regular: int, fixed: list[Entry],
                    bds: np.ndarray) -> list[Entry]:
    """Operating inflows/outflows. Direction and size respond to the balance (see module doc)."""
    by_acct: dict[str, list[Entry]] = {}
    for e in fixed:
        by_acct.setdefault(e.account.account_id, []).append(e)
    out: list[Entry] = []
    for a, k in zip(accounts, _spread(rng, n_regular, accounts)):
        k = int(k)
        if k == 0:
            continue
        lo, hi = np.searchsorted(bds, a.first_bd), np.searchsorted(bds, a.last_bd, side="right")
        days = np.sort(bds[rng.integers(lo, hi, k)])
        fx = sorted(by_acct.get(a.account_id, []), key=lambda e: e.day)
        fdays = np.array([e.day for e in fx], dtype=int)
        fcum = np.cumsum([e.signed for e in fx]) if fx else np.array([], dtype=int)
        target = max(a.opening_balance, rb.usd_to_local_minor(P.BALANCE_TARGET_FLOOR_USD, a.currency, a.first_day))
        reg_net = 0
        median = P.REGULAR_MEDIAN_USD[a.acct_type]
        for day in days:
            day = int(day)
            idx = int(np.searchsorted(fdays, day, side="right"))
            bal = a.opening_balance + (int(fcum[idx - 1]) if idx else 0) + reg_net
            dev = (target - bal) / target
            p_in = 1.0 / (1.0 + np.exp(-dev / P.FEEDBACK_SCALE))
            credit = rng.random() < p_in
            usd = max(P.MIN_AMOUNT_USD, median * float(np.exp(P.REGULAR_SIGMA * rng.standard_normal())))
            if (credit and dev > 0) or (not credit and dev < 0):
                usd *= min(1.0 + abs(dev), P.REGULAR_MAX_CORRECTION)
            amt = rb.usd_to_local_minor(usd, a.currency, day)
            reg_net += amt if credit else -amt
            out.append(Entry(-1, a, day, "CREDIT" if credit else "DEBIT", amt,
                             "OPERATING_INFLOW" if credit else "OPERATING_OUTFLOW",
                             "Operating receipt" if credit else "Operating payment"))
    return out


def reversal_entries(rng, cal, regular: list[Entry], n_rev: int) -> list[Entry]:
    out, used = [], set()
    for k in rng.permutation(len(regular)):
        if len(out) == n_rev:
            break
        o = regular[int(k)]
        day = cal.next_bd(o.day + int(rng.integers(1, P.REVERSAL_MAX_GAP_DAYS + 1)))
        if day > o.account.last_bd:
            continue
        used.add(int(k))
        out.append(Entry(-1, o.account, day, "DEBIT" if o.direction == "CREDIT" else "CREDIT",
                         o.amount, "REVERSAL", f"Reversal of: {o.narrative}", reverses_seq=id(o)))
    if len(out) < n_rev:
        raise ValueError("could not place all reversals inside account windows")
    return out


def build_ledger(cal: Calendar, rb: RateBook, entries: list[Entry]) -> pd.DataFrame:
    """Sort, number and convert. Returns the ledger_entry table; stores ids on entries."""
    by_obj = {id(e): e for e in entries}
    for seq, e in enumerate(entries, start=1):
        e.seq = seq
    for e in entries:
        if e.reverses_seq is not None:
            e.reverses_seq = by_obj[e.reverses_seq].seq
    order = sorted(entries, key=lambda e: (e.day, e.account.account_id, e.seq))
    factory = ids.IdFactory()
    ids_by_seq: dict[int, str] = {}
    for e in order:
        ids_by_seq[e.seq] = factory.ledger_entry(cal.date(e.day).year)
    rows = []
    for e in order:
        cur = e.account.currency
        base = int(rb.to_base_minor(e.amount, cur, e.day))
        rows.append({
            "ledger_entry_id": ids_by_seq[e.seq], "bank_account_id": e.account.account_id,
            "entry_date": cal.iso(e.day), "direction": e.direction, "amount_minor": e.amount,
            "currency": cur, "fx_rate_id": rb.rate_id(cur, e.day), "base_amount_minor": base,
            "entry_type": e.etype, "external_ref": f"BNK{e.seq:09d}", "narrative": e.narrative,
            "reverses_ledger_entry_id": ids_by_seq[e.reverses_seq] if e.reverses_seq else None,
        })
    for e in entries:
        e.ledger_id = ids_by_seq[e.seq]
    return pd.DataFrame(rows)


def item_tables(cal: Calendar, items: list[Item], entries: list[Entry]):
    """receivable, payable, settlement_link, and the two status-event tables."""
    factory = ids.IdFactory()
    doc_n: dict[tuple, int] = {}
    for it in sorted(items, key=lambda i: (i.invoice, i.account.entity_id, i.account.account_id,
                                           i.cparty, i.amount)):
        year = cal.date(it.invoice).year
        it.item_id = factory.receivable(year) if it.kind == "R" else factory.payable(year)
        if it.doc_prefix in ("SINV", "PINV"):
            key = (it.account.entity_id, it.doc_prefix)
            doc_n[key] = doc_n.get(key, 0) + 1
            it.doc_ref = f"{it.doc_prefix}-{doc_n[key]:06d}"
        else:
            it.doc_ref = it.doc_prefix

    entry_id_of, links = {}, []
    for e in entries:
        for it, ti, amt in e.allocs:
            entry_id_of[(id(it), ti)] = e.ledger_id
            links.append((e.ledger_id, it.item_id, it.kind, amt))
    links.sort(key=lambda x: (x[0], x[1]))

    rcv, pay, rev, pev = [], [], [], []
    ev_id = {"R": 0, "P": 0}
    status_of = {}
    for it in sorted(items, key=lambda i: i.item_id):
        evs = item_events(it, entry_id_of)
        status_of[it.item_id] = evs[-1]["to_status"]
        target = rev if it.kind == "R" else pev
        key = "receivable_id" if it.kind == "R" else "payable_id"
        for ev in evs:
            ev_id[it.kind] += 1
            target.append({"event_id": ev_id[it.kind], key: it.item_id, **ev,
                           "event_date": cal.iso(ev["event_date"])})
    for it in sorted(items, key=lambda i: i.item_id):
        base = {"entity_id": it.account.entity_id, "bank_account_id": it.account.account_id,
                "document_ref": it.doc_ref, "currency": it.currency, "amount_minor": it.amount,
                "invoice_date": cal.iso(it.invoice), "due_date": cal.iso(it.due),
                "expected_date": cal.iso(it.expected), "status": status_of[it.item_id]}
        if it.kind == "R":
            rcv.append({"receivable_id": it.item_id, **{k: base[k] for k in ("entity_id", "bank_account_id")},
                        "counterparty_name": it.cparty, "counterparty_risk_tier": it.tier,
                        **{k: base[k] for k in ("document_ref", "currency", "amount_minor", "invoice_date",
                                                "due_date", "expected_date", "status")}})
        else:
            pay.append({"payable_id": it.item_id, **{k: base[k] for k in ("entity_id", "bank_account_id")},
                        "payee_name": it.cparty, "payable_category": it.category,
                        "is_one_off": int(it.one_off),
                        **{k: base[k] for k in ("document_ref", "currency", "amount_minor", "invoice_date",
                                                "due_date", "expected_date", "status")}})
    link_rows = []
    for n, (led, iid, kind, amt) in enumerate(links, start=1):
        link_rows.append({"settlement_link_id": n,
                          "receivable_id": iid if kind == "R" else None,
                          "payable_id": iid if kind == "P" else None,
                          "ledger_entry_id": led, "allocated_amount_minor": amt})
    return (pd.DataFrame(rcv), pd.DataFrame(pay), pd.DataFrame(link_rows),
            pd.DataFrame(rev), pd.DataFrame(pev))
