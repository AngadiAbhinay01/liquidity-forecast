"""Exception queue and decision workflow.

A detection becomes a queue row with a severity, a recommended decision from the playbook
(src/detect/params.py) and an assigned reviewer role. Reviewers then record a decision or
escalate. Every change is one appended row of the decision log; the log has no update or
delete path. Who may decide what:

  * the risk/compliance reviewer may decide any exception, and is the only one who may
    dismiss an exception as a false positive;
  * the operations analyst may decide exceptions assigned to that role; for anything else
    the analyst can only escalate it, which reassigns it to risk/compliance;
  * an exception is decided once. A mistaken decision is handled by a new exception.
"""
from __future__ import annotations

import pandas as pd

from . import params as P

ROLES = ("OPERATIONS_ANALYST", "RISK_COMPLIANCE_REVIEWER")
QUEUE_COLUMNS = ["exception_id", "issue_type", "table_name", "record_id", "severity", "impact_base_minor",
                 "detected_by", "evidence", "recommended_decision", "assigned_role", "status",
                 "reviewer_decision", "decided_by_role", "decision_note"]
LOG_COLUMNS = ["log_id", "exception_id", "event_seq", "event_type", "actor_role", "decision", "note",
               "from_status", "to_status", "assigned_role"]
ALLOWED_DECISIONS = tuple(P.RECOMMENDED_DECISION.values()) + (P.DISMISS_DECISION,)


def severity_for(issue_type: str, impact_base_minor) -> str:
    """Base level per type; one level up when the impact reaches the threshold, never above the cap."""
    i = P.SEVERITY_ORDER.index(P.BASE_SEVERITY[issue_type])
    cap = P.SEVERITY_ORDER.index(P.SEVERITY_BUMP_CAP)
    big = impact_base_minor is not None and not pd.isna(impact_base_minor) \
        and abs(impact_base_minor) / 100.0 >= P.SEVERITY_BUMP_USD
    if big and i < cap:
        i += 1
    return P.SEVERITY_ORDER[i]


def role_for(issue_type: str, severity: str) -> str:
    if severity == "CRITICAL" or issue_type in P.RISK_COMPLIANCE_TYPES:
        return "RISK_COMPLIANCE_REVIEWER"
    return "OPERATIONS_ANALYST"


class ExceptionWorkflow:
    def __init__(self) -> None:
        self._queue: dict[str, dict] = {}
        self._log: list[dict] = []

    # ---- views (copies: callers cannot edit the log) ----------------------------------------------
    @property
    def queue(self) -> pd.DataFrame:
        rows = sorted(self._queue.values(), key=lambda r: (-P.SEVERITY_ORDER.index(r["severity"]), r["exception_id"]))
        return pd.DataFrame(rows, columns=QUEUE_COLUMNS)

    @property
    def log(self) -> pd.DataFrame:
        return pd.DataFrame([dict(r) for r in self._log], columns=LOG_COLUMNS)

    # ---- opening ---------------------------------------------------------------------------------------
    def _append(self, exception_id: str, event_type: str, actor: str, to_status: str,
                decision: str | None = None, note: str | None = None) -> None:
        q = self._queue[exception_id]
        seq = 1 + sum(1 for e in self._log if e["exception_id"] == exception_id)
        prev = None if seq == 1 else [e for e in self._log if e["exception_id"] == exception_id][-1]["to_status"]
        self._log.append({"log_id": len(self._log) + 1, "exception_id": exception_id, "event_seq": seq,
                          "event_type": event_type, "actor_role": actor, "decision": decision, "note": note,
                          "from_status": prev, "to_status": to_status, "assigned_role": q["assigned_role"]})

    def open_from(self, detections: pd.DataFrame) -> "ExceptionWorkflow":
        d = detections.sort_values(["issue_type", "table_name", "record_id"], kind="stable")
        for n, r in enumerate(d.itertuples(index=False), start=1):
            eid = f"EXC-{n:06d}"
            impact = None if pd.isna(r.impact_base_minor) else int(r.impact_base_minor)
            sev = severity_for(r.issue_type, impact)
            self._queue[eid] = {
                "exception_id": eid, "issue_type": r.issue_type, "table_name": r.table_name,
                "record_id": r.record_id, "severity": sev, "impact_base_minor": impact,
                "detected_by": r.detected_by, "evidence": r.evidence,
                "recommended_decision": P.RECOMMENDED_DECISION[r.issue_type],
                "assigned_role": role_for(r.issue_type, sev), "status": "OPEN", "reviewer_decision": None,
                "decided_by_role": None, "decision_note": None}
            self._append(eid, "DETECTED", "SYSTEM", "OPEN", note=r.detected_by)
            self._append(eid, "ROUTED", "SYSTEM", "OPEN",
                         note=f"severity {sev}; assigned to {self._queue[eid]['assigned_role']}")
        return self

    # ---- reviewer actions --------------------------------------------------------------------------------
    def _get(self, exception_id: str) -> dict:
        if exception_id not in self._queue:
            raise KeyError(f"unknown exception {exception_id}")
        return self._queue[exception_id]

    def record_decision(self, exception_id: str, role: str, decision: str, note: str = "") -> None:
        q = self._get(exception_id)
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if decision not in ALLOWED_DECISIONS:
            raise ValueError(f"unknown decision {decision!r}")
        if q["status"] == "DECIDED":
            raise ValueError(f"{exception_id} already has a decision")
        if decision == P.DISMISS_DECISION and role != "RISK_COMPLIANCE_REVIEWER":
            raise PermissionError("only the risk/compliance reviewer may dismiss an exception")
        if role == "OPERATIONS_ANALYST" and q["assigned_role"] != "OPERATIONS_ANALYST":
            raise PermissionError(f"{exception_id} is assigned to {q['assigned_role']}; escalate instead")
        q.update(status="DECIDED", reviewer_decision=decision, decided_by_role=role, decision_note=note or None)
        self._append(exception_id, "DECISION_RECORDED", role, "DECIDED", decision=decision, note=note or None)

    def escalate(self, exception_id: str, role: str, note: str = "") -> None:
        q = self._get(exception_id)
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        if q["status"] == "DECIDED":
            raise ValueError(f"{exception_id} is already decided")
        if q["assigned_role"] == "RISK_COMPLIANCE_REVIEWER":
            raise ValueError(f"{exception_id} is already with risk/compliance")
        q.update(status="ESCALATED", assigned_role="RISK_COMPLIANCE_REVIEWER")
        self._append(exception_id, "ESCALATED", role, "ESCALATED", note=note or None)
