-- Exception queue and decision log (outputs of the detection step, not part of the input schema).
-- The decision log is append-only; the queue row only moves forward through its statuses.
CREATE TABLE exception_queue (
    exception_id          TEXT PRIMARY KEY CHECK (exception_id GLOB 'EXC-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    issue_type            TEXT NOT NULL,
    table_name            TEXT NOT NULL,
    record_id             TEXT NOT NULL,
    severity              TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
    impact_base_minor     INTEGER,
    detected_by           TEXT NOT NULL,
    evidence              TEXT NOT NULL,
    recommended_decision  TEXT NOT NULL,
    assigned_role         TEXT NOT NULL CHECK (assigned_role IN ('OPERATIONS_ANALYST', 'RISK_COMPLIANCE_REVIEWER')),
    status                TEXT NOT NULL CHECK (status IN ('OPEN', 'ESCALATED', 'DECIDED')),
    reviewer_decision     TEXT,
    decided_by_role       TEXT CHECK (decided_by_role IN ('OPERATIONS_ANALYST', 'RISK_COMPLIANCE_REVIEWER')),
    decision_note         TEXT,
    UNIQUE (issue_type, table_name, record_id),
    CHECK ((status = 'DECIDED') = (reviewer_decision IS NOT NULL)),
    CHECK ((reviewer_decision IS NULL) = (decided_by_role IS NULL))
);

CREATE TABLE decision_log (
    log_id          INTEGER PRIMARY KEY,
    exception_id    TEXT    NOT NULL REFERENCES exception_queue (exception_id),
    event_seq       INTEGER NOT NULL CHECK (event_seq >= 1),
    event_type      TEXT    NOT NULL CHECK (event_type IN ('DETECTED', 'ROUTED', 'ESCALATED', 'DECISION_RECORDED')),
    actor_role      TEXT    NOT NULL CHECK (actor_role IN ('SYSTEM', 'OPERATIONS_ANALYST', 'RISK_COMPLIANCE_REVIEWER')),
    decision        TEXT,
    note            TEXT,
    from_status     TEXT,
    to_status       TEXT    NOT NULL CHECK (to_status IN ('OPEN', 'ESCALATED', 'DECIDED')),
    assigned_role   TEXT    NOT NULL,
    UNIQUE (exception_id, event_seq),
    CHECK ((event_type = 'DECISION_RECORDED') = (decision IS NOT NULL))
);

CREATE TRIGGER trg_decision_log_no_update BEFORE UPDATE ON decision_log
BEGIN SELECT RAISE(ABORT, 'decision_log is append-only'); END;
CREATE TRIGGER trg_decision_log_no_delete BEFORE DELETE ON decision_log
BEGIN SELECT RAISE(ABORT, 'decision_log is append-only'); END;
CREATE TRIGGER trg_exception_queue_no_delete BEFORE DELETE ON exception_queue
BEGIN SELECT RAISE(ABORT, 'exceptions are never deleted; record a decision instead'); END;
CREATE TRIGGER trg_exception_queue_frozen_fields BEFORE UPDATE ON exception_queue
WHEN NEW.issue_type <> OLD.issue_type OR NEW.table_name <> OLD.table_name OR NEW.record_id <> OLD.record_id
  OR NEW.severity <> OLD.severity OR NEW.evidence <> OLD.evidence OR NEW.exception_id <> OLD.exception_id
  OR OLD.status = 'DECIDED'
BEGIN SELECT RAISE(ABORT, 'only status, assigned role and the decision fields of an open exception may change'); END;
