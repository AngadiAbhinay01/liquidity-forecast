"""Status history of receivables and payables.

The status of an item is *derived* from its plan: events are produced in date order
(ties: overdue notice, then settlement, then write-off/cancellation) and the stored
status is the last event's ``to_status``. Every transition is checked against the
lifecycle documented in docs/data_dictionary.md.
"""
from __future__ import annotations

ALLOWED = {
    "R": {
        "OPEN": {"PARTIALLY_SETTLED", "OVERDUE", "SETTLED", "CANCELLED"},
        "PARTIALLY_SETTLED": {"OVERDUE", "SETTLED", "WRITTEN_OFF"},
        "OVERDUE": {"PARTIALLY_SETTLED", "SETTLED", "WRITTEN_OFF"},
    },
    "P": {
        "OPEN": {"PARTIALLY_SETTLED", "OVERDUE", "SETTLED", "CANCELLED"},
        "PARTIALLY_SETTLED": {"OVERDUE", "SETTLED"},
        "OVERDUE": {"PARTIALLY_SETTLED", "SETTLED"},
    },
}
TERMINAL_STATUS = {"WRITE_OFF": "WRITTEN_OFF", "CANCELLATION": "CANCELLED"}
TERMINAL_ACTOR = {"WRITE_OFF": "RISK_COMPLIANCE_REVIEWER", "CANCELLATION": "OPERATIONS_ANALYST"}


def item_events(item, entry_id_of: dict) -> list[dict]:
    """Ordered events for one item. ``entry_id_of[(id(item), tranche_index)]`` is the
    ledger entry that carried that tranche."""
    kind = item.kind
    raw = [(item.invoice, -1, "CREATED", "OPEN", None, "SYSTEM")]
    cum_by_due = sum(a for d, a in item.tranches if d <= item.due)
    term_day = item.terminal[1] if item.terminal else None
    if (cum_by_due < item.amount and item.due + 1 <= item.account.last_day
            and (term_day is None or term_day > item.due)):
        raw.append((item.due + 1, 0, "DUE_DATE_PASSED", "OVERDUE", None, "SYSTEM"))
    for i, (d, a) in enumerate(item.tranches):
        raw.append((d, 1, "SETTLEMENT", None, entry_id_of[(id(item), i)], None))
    if item.terminal:
        cause, d = item.terminal
        raw.append((d, 2, cause, TERMINAL_STATUS[cause], None, TERMINAL_ACTOR[cause]))
    raw.sort(key=lambda e: (e[0], e[1]))

    out, status, cum, tr = [], None, 0, 0
    for day, _, cause, to, led, actor in raw:
        if cause == "SETTLEMENT":
            amount = item.tranches[tr][1]
            tr += 1
            cum += amount
            full = cum >= item.amount
            to = "SETTLED" if full else "PARTIALLY_SETTLED"
            cause = "FULL_SETTLEMENT" if full else "PARTIAL_SETTLEMENT"
            actor = "CUSTOMER" if kind == "R" else "SYSTEM"
            if to == status:          # a second partial tranche: status unchanged, no event
                continue
        if status is not None and to not in ALLOWED[kind].get(status, set()):
            raise ValueError(f"illegal transition {status}->{to} for {item.doc_ref}")
        out.append({"event_seq": len(out) + 1, "from_status": status, "to_status": to,
                    "event_date": day, "cause": cause, "ledger_entry_id": led, "actor_role": actor})
        status = to
    return out
