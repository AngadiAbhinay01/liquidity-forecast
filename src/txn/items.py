"""Receivables and payables: scheduled amounts, dates, counterparties and the
settlement tranches that later become ledger entries.

An *item* is one receivable or payable. Its plan is a list of settlement tranches
``(day, amount)`` plus an optional terminal event (write-off or cancellation). The
ledger rows and the status history are derived from that plan afterwards; the
stored ``status`` is the last event of the history, never drawn separately.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import Config
from ..master import params as MP
from . import params as P
from .common import Calendar, RateBook

R, PAY = "R", "P"          # item kinds


@dataclass
class Acct:
    account_id: str
    entity_id: str
    entity_seq: int
    account_seq: int
    currency: str
    acct_type: str
    status: str
    opening_balance: int
    first_day: int         # opening date
    last_day: int          # min(effective_to, end of window)
    first_bd: int
    last_bd: int
    weight: float


@dataclass
class Item:
    kind: str
    account: Acct
    cparty: str
    tier: str | None
    category: str | None
    one_off: bool
    doc_prefix: str
    currency: str
    amount: int
    invoice: int
    due: int
    expected: int
    tranches: list[list[int]] = field(default_factory=list)      # [day, amount]
    terminal: tuple[str, int] | None = None                      # (cause, day)
    batchable: bool = False
    # assigned later
    item_id: str = ""
    doc_ref: str = ""


def build_accounts(cfg: Config, cal: Calendar, accounts: pd.DataFrame, entities: pd.DataFrame) -> list[Acct]:
    """Posting window of each account: from opening to closure, and for a suspended entity only up to the
    day before the suspension (a suspended entity posts nothing new; its balances stay on the books)."""
    end = cfg.n_days - 1
    suspended = {e.entity_id: cal.offset(e.suspended_from) - 1
                 for e in entities.itertuples(index=False) if isinstance(e.suspended_from, str)}
    out = []
    for r in accounts.itertuples(index=False):
        first = cal.offset(r.opening_date)
        last = min(cal.offset(r.effective_to), end) if isinstance(r.effective_to, str) else end
        last = min(last, suspended.get(r.entity_id, end))
        first_bd, last_bd = cal.next_bd(first), cal.prev_bd(last)
        if last_bd < first_bd:
            continue
        w = float(last_bd - first_bd + 1) * (P.DORMANT_ACTIVITY_FACTOR if r.status == "DORMANT" else 1.0)
        out.append(Acct(r.bank_account_id, r.entity_id, int(r.entity_id[4:]), int(r.account_seq),
                        r.currency, r.account_type, r.status, int(r.opening_balance_minor),
                        first, last, first_bd, last_bd, w))
    return out


def _lognormal_usd(rng, median: float, sigma: float) -> float:
    return max(P.MIN_AMOUNT_USD, float(median * np.exp(sigma * rng.standard_normal())))


def _geom(rng, mean: float, cap: int) -> int:
    return int(min(cap, rng.geometric(1.0 / mean)))


class Counterparties:
    """Per-entity customer and supplier pools: invented names, fixed risk tiers for
    customers, Zipf weights (a few large counterparties, a long tail)."""

    def __init__(self, rng, entity_ids: list[str]):
        tiers = list(P.RISK_TIER_WEIGHTS)
        tier_p = np.array(list(P.RISK_TIER_WEIGHTS.values()))
        self.customers: dict[str, tuple[list[str], list[str], np.ndarray]] = {}
        self.suppliers: dict[str, tuple[list[str], np.ndarray]] = {}
        for e in entity_ids:
            names = self._names(rng, P.CUSTOMERS_PER_ENTITY)
            tier = [tiers[i] for i in rng.choice(len(tiers), P.CUSTOMERS_PER_ENTITY, p=tier_p)]
            self.customers[e] = (names, tier, self._zipf(P.CUSTOMERS_PER_ENTITY))
            self.suppliers[e] = (self._names(rng, P.SUPPLIERS_PER_ENTITY),
                                 self._zipf(P.SUPPLIERS_PER_ENTITY))

    @staticmethod
    def _zipf(n: int) -> np.ndarray:
        w = 1.0 / np.arange(1, n + 1)
        return w / w.sum()

    @staticmethod
    def _names(rng, n: int) -> list[str]:
        used: set[str] = set()
        while len(used) < n:
            k = int(rng.integers(2, 4))
            stem = "".join(MP.NAME_SYLLABLES[int(i)] for i in rng.integers(0, len(MP.NAME_SYLLABLES), k))
            word = MP.INDUSTRY_WORDS[int(rng.integers(0, len(MP.INDUSTRY_WORDS)))]
            used.add(f"{stem.capitalize()} {word}")
        # sort first: set order depends on Python's per-process string hash seed, and each name
        # draws a random key in iteration order, so an unsorted start would change the result per process
        return sorted(sorted(used), key=lambda s: rng.random())


def _split(rng, amount: int, n: int) -> list[int]:
    """Split ``amount`` into n positive tranches (first U(.4,.8), second U(.3,.7) of the rest)."""
    first = int(round(amount * rng.uniform(*P.PARTIAL_FIRST_FRACTION)))
    first = min(max(first, 1), amount - n + 1)
    parts = [first]
    rest = amount - first
    if n == 3:
        second = int(round(rest * rng.uniform(*P.PARTIAL_SECOND_FRACTION)))
        second = min(max(second, 1), rest - 1)
        parts.append(second)
        rest -= second
    parts.append(rest)
    return parts


def _finish(item: Item, cal: Calendar, drop_first_ok: bool = True) -> Item | None:
    """Drop tranches outside the account's posting window and apply closure rules."""
    a = item.account
    if item.tranches and item.tranches[0][0] < a.first_bd:
        return None                                   # would settle before the account existed
    item.tranches = [t for t in item.tranches if t[0] <= a.last_bd]
    if item.terminal and item.terminal[1] > a.last_day:
        item.terminal = None
    settled = sum(t[1] for t in item.tranches)
    if settled < item.amount and item.terminal is None and a.status == "CLOSED":
        last = a.last_day
        if item.kind == R and (item.tranches or last > item.due):
            item.terminal = ("WRITE_OFF", last)
        elif item.kind == PAY and not item.tranches and last <= item.due:
            item.terminal = ("CANCELLATION", last)
    item.batchable = (item.kind == R or item.category == "TRADE") and len(item.tranches) == 1 \
        and item.terminal is None and item.tranches[0][1] == item.amount and not item.one_off
    return item


def draw_receivable(rng, cal: Calendar, rb: RateBook, a: Acct, cp: Counterparties) -> Item | None:
    names, tiers, w = cp.customers[a.entity_id]
    k = int(rng.choice(len(names), p=w))
    tier = tiers[k]
    lo = a.first_day - P.PRE_WINDOW_DAYS if a.first_day == 0 else a.first_day
    hi = a.last_bd - 1
    if hi < lo:
        return None
    inv = int(rng.integers(lo, hi + 1))
    terms = int(rng.choice(P.TERMS_DAYS, p=P.TERMS_WEIGHTS))
    due = inv + terms
    exp = cal.next_bd(due)
    usd = _lognormal_usd(rng, P.RECEIVABLE_MEDIAN_USD, P.RECEIVABLE_SIGMA)
    amount = rb.usd_to_local_minor(usd, a.currency, max(inv, 0))
    item = Item(R, a, names[k], tier, None, False, "SINV", a.currency, amount, inv, due, exp)

    u = rng.random()
    if u < P.CANCEL_PROB:
        item.terminal = ("CANCELLATION", inv + int(rng.integers(P.CANCEL_WITHIN_DAYS[0],
                                                                  P.CANCEL_WITHIN_DAYS[1] + 1)))
        return _finish(item, cal)
    if u < P.CANCEL_PROB + P.WRITE_OFF_PROB[tier]:
        item.terminal = ("WRITE_OFF", due + P.WRITE_OFF_AFTER_DUE_DAYS)
        return _finish(item, cal)

    if rng.random() < P.LATE_PROB[tier]:
        d = exp + _geom(rng, P.LATE_MEAN_DAYS[tier], P.LATE_MAX_DAYS)
    elif rng.random() < P.EARLY_PROB:
        d = max(exp - int(rng.integers(1, P.EARLY_MAX_DAYS + 1)), inv + 1)
    else:
        d = exp
    d = cal.next_bd(d)

    if rng.random() < P.PARTIAL_PROB[tier] and amount >= 3:
        n = 3 if rng.random() < P.PARTIAL_THIRD_TRANCHE_PROB and amount >= 5 else 2
        parts = _split(rng, amount, n)
        wo = tier == "HIGH" and rng.random() < P.PARTIAL_WRITE_OFF_PROB_HIGH
        if wo:
            parts = parts[:-1]                         # the rest is never paid
        days = [d]
        for _ in parts[1:]:
            days.append(cal.next_bd(days[-1] + _geom(rng, P.PARTIAL_GAP_MEAN_DAYS, 90)))
        item.tranches = [[dd, pp] for dd, pp in zip(days, parts)]
        if wo:
            item.terminal = ("WRITE_OFF", max(due + P.WRITE_OFF_AFTER_DUE_DAYS, days[-1] + 1))
    else:
        item.tranches = [[d, amount]]
    return _finish(item, cal)


def draw_trade_payable(rng, cal: Calendar, rb: RateBook, a: Acct, cp: Counterparties) -> Item | None:
    names, w = cp.suppliers[a.entity_id]
    k = int(rng.choice(len(names), p=w))
    lo = a.first_day - P.PRE_WINDOW_DAYS if a.first_day == 0 else a.first_day
    hi = a.last_bd - 1
    if hi < lo:
        return None
    inv = int(rng.integers(lo, hi + 1))
    terms = int(rng.choice(P.TERMS_DAYS, p=P.TERMS_WEIGHTS))
    due = inv + terms
    exp = cal.next_bd(due)
    usd = _lognormal_usd(rng, P.PAYABLE_MEDIAN_USD, P.PAYABLE_SIGMA)
    amount = rb.usd_to_local_minor(usd, a.currency, max(inv, 0))
    item = Item(PAY, a, names[k], None, "TRADE", False, "PINV", a.currency, amount, inv, due, exp)
    if rng.random() < P.PAYABLE_CANCEL_PROB:
        item.terminal = ("CANCELLATION", inv + int(rng.integers(1, 6)))
        return _finish(item, cal)
    timing = int(rng.choice(3, p=P.PAYABLE_TIMING_WEIGHTS))
    if timing == 0:
        d = exp
    elif timing == 1:
        d = max(exp - int(rng.integers(1, P.PAYABLE_EARLY_MAX_DAYS + 1)), inv + 1)
    else:
        d = exp + _geom(rng, P.PAYABLE_LATE_MEAN_DAYS, 45)
    d = cal.next_bd(d)
    if rng.random() < P.PAYABLE_PARTIAL_PROB and amount >= 3:
        parts = _split(rng, amount, 2)
        d2 = cal.next_bd(d + _geom(rng, P.PARTIAL_GAP_MEAN_DAYS, 60))
        item.tranches = [[d, parts[0]], [d2, parts[1]]]
    else:
        item.tranches = [[d, amount]]
    return _finish(item, cal)


def _month_starts(cfg: Config, cal: Calendar) -> list[tuple[int, int, int]]:
    """(year, month, day offset of the 1st) for every month touching the window."""
    out, y, m = [], cal.start.year, cal.start.month
    while True:
        off = (cal.date(0).replace(year=y, month=m, day=1) - cal.start).days
        if off >= cfg.n_days:
            break
        out.append((y, m, off))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def scheduled_payables(cfg: Config, rng, cal: Calendar, rb: RateBook,
                       accounts: list[Acct], entities: pd.DataFrame) -> list[Item]:
    """Payroll (monthly), debt service (monthly, some entities), tax (quarterly) and
    one-off large payments. Each is paid on its expected date."""
    items: list[Item] = []
    months = _month_starts(cfg, cal)
    by_entity: dict[str, list[Acct]] = {}
    for a in accounts:
        by_entity.setdefault(a.entity_id, []).append(a)

    def add(a: Acct, payee: str, cat: str, one_off: bool, prefix: str, usd: float,
            invoice: int, due: int) -> None:
        exp = cal.next_bd(due)
        amount = rb.usd_to_local_minor(usd, a.currency, max(invoice, 0))
        item = Item(PAY, a, payee, None, cat, one_off, prefix, a.currency, amount,
                    invoice, due, exp, tranches=[[exp, amount]])
        done = _finish(item, cal)
        if done is not None:
            items.append(done)

    for a in accounts:
        if a.acct_type != "PAYROLL":
            continue
        base = _lognormal_usd(rng, P.PAYROLL_MEDIAN_USD, P.PAYROLL_SIGMA)
        for y, m, off in months:
            day = off + P.PAYROLL_PAY_DAY - 1
            day = cal.prev_bd(day)
            if not a.first_bd <= day <= a.last_bd:
                continue
            usd = base * (1 + P.PAYROLL_MONTH_NOISE * rng.standard_normal())
            add(a, f"Payroll {a.entity_id}", "PAYROLL", False, f"PAYROLL-{y}{m:02d}-{a.account_seq:03d}",
                usd, day, day)

    debt_entities = [e for e in entities.entity_id if e in by_entity]
    pick = rng.random(len(debt_entities)) < P.DEBT_SERVICE_ENTITY_SHARE
    for e, chosen in zip(debt_entities, pick):
        ops = [a for a in by_entity[e] if a.acct_type in ("OPERATING", "DISBURSEMENT")]
        if not chosen or not ops:
            continue
        a = ops[0]
        usd = _lognormal_usd(rng, P.DEBT_SERVICE_MEDIAN_USD, P.DEBT_SERVICE_SIGMA)
        for y, m, off in months:
            day = off + P.DEBT_SERVICE_DAY - 1
            if cal.next_bd(day) <= a.last_bd:
                add(a, f"Lender of {e}", "DEBT_SERVICE", False, f"DEBT-{y}{m:02d}", usd, day, day)

    for e in entities.entity_id:
        ops = [a for a in by_entity.get(e, []) if a.acct_type == "OPERATING"]
        if not ops:
            continue
        a = ops[0]
        usd = _lognormal_usd(rng, P.TAX_MEDIAN_USD, P.TAX_SIGMA)
        for y, m, off in months:
            if m in P.TAX_MONTHS:
                day = off + P.TAX_DAY - 1
                q = (m - 1) // 3 + 1
                add(a, "Tax authority", "TAX", False, f"TAX-{y}Q{q}",
                    usd * float(np.exp(0.1 * rng.standard_normal())), day - 14, day)

    live = [e for e in entities.itertuples(index=False)
            if e.status in ("ACTIVE", "SUSPENDED") and e.entity_id in by_entity]
    n_one_off = max(P.ONE_OFF_MIN_COUNT, round(P.ONE_OFF_PER_ENTITY * cfg.n_entities))
    for k in range(n_one_off):
        e = live[int(rng.integers(0, len(live)))]
        a = [x for x in by_entity[e.entity_id] if x.acct_type in ("OPERATING", "DISBURSEMENT")]
        a = a[0] if a else by_entity[e.entity_id][0]
        inv = int(rng.integers(max(a.first_bd, 15), max(a.first_bd, a.last_bd - P.ONE_OFF_TERMS_DAYS - 1) + 1))
        cat = "CAPEX" if rng.random() < P.ONE_OFF_CAPEX_SHARE else "OTHER"
        usd = _lognormal_usd(rng, P.ONE_OFF_MEDIAN_USD, P.ONE_OFF_SIGMA)
        add(a, f"One-off supplier {k + 1:03d}", cat, True, f"ONEOFF-{k + 1:03d}", usd,
            inv, inv + P.ONE_OFF_TERMS_DAYS)
    return items


def generate_trade_items(cfg: Config, rng, cal: Calendar, rb: RateBook, accounts: list[Acct],
                         cp: Counterparties, n_receivables: int, n_payables: int) -> list[Item]:
    items: list[Item] = []
    for kind, n, types, draw in ((R, n_receivables, ("OPERATING", "COLLECTION"), draw_receivable),
                                 (PAY, n_payables, ("OPERATING", "DISBURSEMENT"), draw_trade_payable)):
        pool = [a for a in accounts if a.acct_type in types]
        w = np.array([a.weight for a in pool])
        w = w / w.sum()
        for k in rng.choice(len(pool), n, p=w):
            it = draw(rng, cal, rb, pool[int(k)], cp)
            if it is not None:
                items.append(it)
    return items
