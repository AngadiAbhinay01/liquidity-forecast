"""Scenario-independent forecast candidates for one cutoff.

Everything here may look only at data dated on or before the cutoff:
  * documents (receivables/payables) that exist by then, their payments to date
    (ledger entries <= cutoff) and their status from events <= cutoff;
  * trailing regular flows (ledger entries <= cutoff) for run-rate items.
Scheduled commitments (payroll, debt service, tax, one-off payments) are known
SCHEDULE_NOTICE_DAYS before they are paid, which stands in for a commitment calendar.
Which documents are included, and their order, never depend on the scenario.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..txn.common import Calendar, RateBook
from . import params as P

SCHEDULED_CATEGORIES = ("PAYROLL", "DEBT_SERVICE", "TAX")


class Knowledge:
    def __init__(self, cal: Calendar, rb: RateBook, tables: dict[str, pd.DataFrame], n_days: int):
        self.cal, self.rb, self.end = cal, rb, n_days - 1
        day = cal.offset
        acc = tables["bank_account"]
        self.accounts = pd.DataFrame({
            "account_id": acc.bank_account_id, "entity_id": acc.entity_id, "currency": acc.currency,
            "open_day": acc.opening_date.map(day),
            "last_day": [min(day(e), self.end) if isinstance(e, str) else self.end for e in acc.effective_to],
        }).set_index("account_id")

        r, p = tables["receivable"], tables["payable"]
        cols = ["doc_id", "source_type", "account_id", "currency", "amount", "invoice_day", "due_day",
                "expected_day", "category", "is_one_off"]
        rd = pd.DataFrame({"doc_id": r.receivable_id, "source_type": "RECEIVABLE", "account_id": r.bank_account_id,
                           "currency": r.currency, "amount": r.amount_minor,
                           "invoice_day": r.invoice_date.map(day), "due_day": r.due_date.map(day),
                           "expected_day": r.expected_date.map(day), "category": None, "is_one_off": 0})
        pdn = pd.DataFrame({"doc_id": p.payable_id, "source_type": "PAYABLE", "account_id": p.bank_account_id,
                            "currency": p.currency, "amount": p.amount_minor,
                            "invoice_day": p.invoice_date.map(day), "due_day": p.due_date.map(day),
                            "expected_day": p.expected_date.map(day), "category": p.payable_category,
                            "is_one_off": p.is_one_off})
        self.docs = pd.concat([rd, pdn], ignore_index=True)[cols]
        scheduled = self.docs.category.isin(SCHEDULED_CATEGORIES) | (self.docs.is_one_off == 1)
        self.docs["known_day"] = np.where(
            scheduled, np.minimum(self.docs.invoice_day, self.docs.due_day - P.SCHEDULE_NOTICE_DAYS),
            self.docs.invoice_day)

        led = tables["ledger_entry"].assign(day=tables["ledger_entry"].entry_date.map(day))
        led_day = led.set_index("ledger_entry_id").day
        links = tables["settlement_link"]
        links = links.assign(doc_id=links.receivable_id.fillna(links.payable_id))
        links["led_day"] = links.ledger_entry_id.map(led_day)
        self.links = links[["doc_id", "ledger_entry_id", "led_day", "allocated_amount_minor"]]
        ev = pd.concat([
            tables["receivable_status_event"].rename(columns={"receivable_id": "doc_id"}),
            tables["payable_status_event"].rename(columns={"payable_id": "doc_id"})], ignore_index=True)
        ev["day"] = ev.event_date.map(day)
        self.events = ev[["doc_id", "event_seq", "day", "to_status"]]
        self.ledger = led[["ledger_entry_id", "bank_account_id", "day", "direction", "amount_minor", "entry_type"]]
        pos = tables["daily_cash_position"]
        self.positions = pos.assign(day=pos.position_date.map(day))[
            ["bank_account_id", "day", "closing_minor", "currency"]]

    # ---- helpers ------------------------------------------------------------------------
    def usd(self, amount: pd.Series, currency: pd.Series, day: int) -> np.ndarray:
        out = np.zeros(len(amount))
        for cur in currency.unique():
            m = (currency == cur).to_numpy()
            out[m] = self.rb.to_base_minor(amount.to_numpy()[m], cur, day) / 100.0
        return out

    # ---- documents -------------------------------------------------------------------------
    def document_candidates(self, c: int, horizon: int) -> pd.DataFrame:
        d = self.docs
        d = d[d.known_day <= c]
        links = self.links[self.links.led_day <= c]
        paid = links.groupby("doc_id").allocated_amount_minor.sum()
        last = (links.sort_values(["led_day", "ledger_entry_id"]).groupby("doc_id").tail(1)
                .set_index("doc_id")[["ledger_entry_id", "led_day"]])
        ev = self.events[self.events.day <= c].sort_values(["doc_id", "day", "event_seq"])
        status = ev.groupby("doc_id").tail(1).set_index("doc_id").to_status
        d = d.assign(paid=d.doc_id.map(paid).fillna(0).astype(np.int64),
                     status=d.doc_id.map(status).fillna("OPEN"))
        d["outstanding"] = d.amount - d.paid
        d = d[(d.outstanding > 0) & ~d.status.isin(["SETTLED", "WRITTEN_OFF", "CANCELLED"])]

        first_open = self.cal.next_bd(c + 1)
        d = d.assign(base_day=np.maximum(d.expected_day, first_open))
        d = d[(d.base_day > c) & (d.base_day <= c + horizon)]
        overdue = c - d.expected_day
        d = d[overdue <= P.OVERDUE_MAX_DAYS]
        d = d.join(self.accounts[["entity_id", "last_day", "open_day"]], on="account_id")
        d = d[(d.open_day <= c) & (d.last_day >= c) & (d.base_day <= d.last_day)]
        d = d[self.usd(d.outstanding, d.currency, c) >= P.FORECAST_MATERIALITY_USD]

        led_id = d.doc_id.map(last.ledger_entry_id)
        led_day = d.doc_id.map(last.led_day)
        out = pd.DataFrame({
            "key": d.account_id + "|" + d.source_type + "|" + d.doc_id,
            "account_id": d.account_id, "entity_id": d.entity_id, "source_type": d.source_type,
            "receivable_id": d.doc_id.where(d.source_type == "RECEIVABLE"),
            "payable_id": d.doc_id.where(d.source_type == "PAYABLE"),
            "source_ledger_entry_id": led_id,
            "source_data_day": np.maximum(d.known_day, led_day.fillna(-10**6)).astype(int),
            "direction": np.where(d.source_type == "RECEIVABLE", "CREDIT", "DEBIT"),
            "currency": d.currency, "expected_day": d.expected_day, "base_day": d.base_day,
            "outstanding": d.outstanding.astype(np.int64), "is_one_off": d.is_one_off.astype(int),
            "last_day": d.last_day})
        return out

    # ---- run-rate -----------------------------------------------------------------------------
    def run_rate_candidates(self, c: int, horizon: int) -> pd.DataFrame:
        lo = c - P.RUN_RATE_LOOKBACK_DAYS
        led = self.ledger[(self.ledger.day > lo) & (self.ledger.day <= c)
                          & self.ledger.entry_type.isin(["OPERATING_INFLOW", "OPERATING_OUTFLOW"])]
        grp = led.groupby(["bank_account_id", "direction"])
        total = grp.amount_minor.sum()
        last = led.sort_values(["day", "ledger_entry_id"]).groupby(["bank_account_id", "direction"]).tail(1) \
            .set_index(["bank_account_id", "direction"])
        rows = []
        for (acct, direction), amount in total.items():
            a = self.accounts.loc[acct]
            if not (a.open_day <= c <= a.last_day):
                continue
            history = min(P.RUN_RATE_LOOKBACK_DAYS, c - a.open_day + 1)
            if history < P.RUN_RATE_MIN_HISTORY_DAYS:
                continue
            daily = amount / history
            src = last.loc[(acct, direction)]
            for block in range(-(-horizon // P.RUN_RATE_BLOCK_DAYS)):
                start = c + 1 + block * P.RUN_RATE_BLOCK_DAYS
                end = min(start + P.RUN_RATE_BLOCK_DAYS - 1, c + horizon, a.last_day)
                if end < start:
                    continue
                n = end - start + 1
                day = self.cal.next_bd(start + n // 2)
                if day > a.last_day:
                    continue
                amt = int(round(daily * n))
                if amt <= 0:
                    continue
                rows.append((f"{acct}|RUN_RATE|{direction}-{block}", acct, a.entity_id,
                             "RUN_RATE", None, None, src.ledger_entry_id, int(src.day), direction,
                             a.currency, day, day, amt, 0, int(a.last_day)))
        cols = ["key", "account_id", "entity_id", "source_type", "receivable_id", "payable_id",
                "source_ledger_entry_id", "source_data_day", "direction", "currency", "expected_day",
                "base_day", "outstanding", "is_one_off", "last_day"]
        return pd.DataFrame(rows, columns=cols)

    def candidates(self, c: int, horizon: int) -> pd.DataFrame:
        parts = [self.document_candidates(c, horizon), self.run_rate_candidates(c, horizon)]
        out = pd.concat(parts, ignore_index=True)
        out["usd_size"] = self.usd(out.outstanding, out.currency, c)
        return out.sort_values("key", kind="stable").reset_index(drop=True)

    # ---- opening position ------------------------------------------------------------------------
    def opening_positions(self, c: int) -> pd.DataFrame:
        p = self.positions[self.positions.day == c]
        return p.join(self.accounts[["entity_id"]], on="bank_account_id")
