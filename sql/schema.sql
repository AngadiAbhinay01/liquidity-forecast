-- =============================================================================
-- Liquidity forecasting dataset: SQLite schema
-- =============================================================================
-- Conventions
--   * Money is stored as INTEGER minor units (cents, paise, yen = exponent 0).
--     Exponent per currency lives in currency.minor_unit_exponent.
--   * Dates are ISO-8601 TEXT 'YYYY-MM-DD'. No wall-clock timestamps anywhere:
--     every date is a simulated business date, so output is reproducible.
--   * FX rates are "base currency units per 1 unit of quote currency".
--     base_amount_minor = round_half_even(amount_minor * rate * 10^(exp_base - exp_quote)).
--   * Schema CHECKs enforce STRUCTURAL rules (formats, enums, signs, row-level
--     date order, ID/column consistency). Cross-row and cross-table BUSINESS
--     rules (reconciliation, currency match, forecast cutoff, duplicates) are
--     deliberately NOT constraints: they are checked by the views at the bottom
--     and by src/validate, so the five labelled anomalies can load and be
--     detected instead of being rejected at insert time.
--   * Append-only tables are protected by triggers (UPDATE/DELETE abort).
--   * Anomalies are injected in memory BEFORE loading; nothing UPDATEs rows
--     after load.
--   * ground_truth is NOT in this database. It is a separate file.
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- 0. Lookup: currency
-- -----------------------------------------------------------------------------
CREATE TABLE currency (
    currency_code        TEXT    PRIMARY KEY CHECK (currency_code GLOB '[A-Z][A-Z][A-Z]'),
    currency_name        TEXT    NOT NULL,
    minor_unit_exponent  INTEGER NOT NULL CHECK (minor_unit_exponent IN (0, 2, 3)),
    is_base              INTEGER NOT NULL DEFAULT 0 CHECK (is_base IN (0, 1))
);
-- Exactly one base (reporting) currency.
CREATE UNIQUE INDEX ux_currency_single_base ON currency (is_base) WHERE is_base = 1;

-- -----------------------------------------------------------------------------
-- 1. LegalEntity            (master)   ENT-000001
-- -----------------------------------------------------------------------------
CREATE TABLE legal_entity (
    entity_id                        TEXT    PRIMARY KEY
        CHECK (entity_id GLOB 'ENT-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    legal_name                       TEXT    NOT NULL UNIQUE,
    country_code                     TEXT    NOT NULL CHECK (country_code GLOB '[A-Z][A-Z]'),
    functional_currency              TEXT    NOT NULL REFERENCES currency (currency_code),
    entity_type                      TEXT    NOT NULL
        CHECK (entity_type IN ('OPERATING', 'HOLDING', 'TREASURY_CENTRE')),
    parent_entity_id                 TEXT    REFERENCES legal_entity (entity_id),
    min_liquidity_buffer_base_minor  INTEGER NOT NULL DEFAULT 0
        CHECK (min_liquidity_buffer_base_minor >= 0),
    status                           TEXT    NOT NULL
        CHECK (status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')),
    effective_from                   TEXT    NOT NULL CHECK (date(effective_from) = effective_from),
    effective_to                     TEXT    CHECK (effective_to IS NULL
                                                    OR (date(effective_to) = effective_to
                                                        AND effective_to >= effective_from)),
    suspended_from                   TEXT    CHECK (suspended_from IS NULL
                                                    OR (date(suspended_from) = suspended_from
                                                        AND suspended_from > effective_from)),
    CHECK (parent_entity_id IS NULL OR parent_entity_id <> entity_id),
    CHECK ((status = 'CLOSED') = (effective_to IS NOT NULL)),
    CHECK ((status = 'SUSPENDED') = (suspended_from IS NOT NULL))
);
CREATE INDEX ix_legal_entity_parent ON legal_entity (parent_entity_id);

-- -----------------------------------------------------------------------------
-- 2. BankAccount            (master)   BANKACC-<entity-id>-001
--    Literal reading of the brief: <entity-id> is the full ENT-000001 string,
--    giving e.g. BANKACC-ENT-000001-001. Enforced by the CHECK below.
-- -----------------------------------------------------------------------------
CREATE TABLE bank_account (
    bank_account_id        TEXT    PRIMARY KEY,
    entity_id              TEXT    NOT NULL REFERENCES legal_entity (entity_id),
    account_seq            INTEGER NOT NULL CHECK (account_seq BETWEEN 1 AND 999),
    bank_name              TEXT    NOT NULL,
    account_type           TEXT    NOT NULL
        CHECK (account_type IN ('OPERATING', 'COLLECTION', 'DISBURSEMENT', 'PAYROLL', 'RESERVE')),
    currency               TEXT    NOT NULL REFERENCES currency (currency_code),
    opening_balance_minor  INTEGER NOT NULL CHECK (opening_balance_minor >= 0),
    opening_date           TEXT    NOT NULL CHECK (date(opening_date) = opening_date),
    status                 TEXT    NOT NULL CHECK (status IN ('ACTIVE', 'DORMANT', 'CLOSED')),
    effective_from         TEXT    NOT NULL CHECK (date(effective_from) = effective_from),
    effective_to           TEXT    CHECK (effective_to IS NULL
                                          OR (date(effective_to) = effective_to
                                              AND effective_to >= effective_from)),
    UNIQUE (entity_id, account_seq),
    CHECK (bank_account_id = 'BANKACC-' || entity_id || '-' || printf('%03d', account_seq)),
    CHECK (opening_date >= effective_from),
    CHECK ((status = 'CLOSED') = (effective_to IS NOT NULL))
);
CREATE INDEX ix_bank_account_entity ON bank_account (entity_id);

-- -----------------------------------------------------------------------------
-- 3. CurrencyRate           (master, effective-dated by rate_date)
--    One row per non-base currency per calendar day. FXR-EUR-20260105
-- -----------------------------------------------------------------------------
CREATE TABLE currency_rate (
    rate_id         TEXT    PRIMARY KEY
        CHECK (rate_id GLOB 'FXR-[A-Z][A-Z][A-Z]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    rate_date       TEXT    NOT NULL CHECK (date(rate_date) = rate_date),
    quote_currency  TEXT    NOT NULL REFERENCES currency (currency_code),
    base_currency   TEXT    NOT NULL REFERENCES currency (currency_code),
    rate            REAL    NOT NULL CHECK (rate > 0),
    rate_source     TEXT    NOT NULL
        CHECK (rate_source IN ('SYNTHETIC_RANDOM_WALK', 'SYNTHETIC_SHOCK')),
    UNIQUE (quote_currency, base_currency, rate_date),
    CHECK (quote_currency <> base_currency),
    CHECK (rate_id = 'FXR-' || quote_currency || '-' || replace(rate_date, '-', ''))
);
CREATE INDEX ix_currency_rate_date ON currency_rate (rate_date);

-- -----------------------------------------------------------------------------
-- 4. Receivable             (transaction)   RCV-2026-000001
-- -----------------------------------------------------------------------------
CREATE TABLE receivable (
    receivable_id           TEXT    PRIMARY KEY
        CHECK (receivable_id GLOB 'RCV-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    entity_id               TEXT    NOT NULL REFERENCES legal_entity (entity_id),
    bank_account_id         TEXT    NOT NULL REFERENCES bank_account (bank_account_id),
    counterparty_name       TEXT    NOT NULL,
    counterparty_risk_tier  TEXT    NOT NULL CHECK (counterparty_risk_tier IN ('LOW', 'MEDIUM', 'HIGH')),
    document_ref            TEXT    NOT NULL,
    currency                TEXT    NOT NULL REFERENCES currency (currency_code),
    amount_minor            INTEGER NOT NULL CHECK (amount_minor > 0),
    invoice_date            TEXT    NOT NULL CHECK (date(invoice_date) = invoice_date),
    due_date                TEXT    NOT NULL CHECK (date(due_date) = due_date),
    expected_date           TEXT    NOT NULL CHECK (date(expected_date) = expected_date),
    status                  TEXT    NOT NULL
        CHECK (status IN ('OPEN', 'PARTIALLY_SETTLED', 'OVERDUE', 'SETTLED', 'WRITTEN_OFF', 'CANCELLED')),
    UNIQUE (entity_id, document_ref),
    CHECK (due_date >= invoice_date),
    CHECK (expected_date >= invoice_date),
    CHECK (substr(receivable_id, 5, 4) = substr(invoice_date, 1, 4))
);
CREATE INDEX ix_receivable_account ON receivable (bank_account_id, expected_date);
CREATE INDEX ix_receivable_entity  ON receivable (entity_id);

-- -----------------------------------------------------------------------------
-- 5. Payable                (transaction)   PAY-2026-000001
-- -----------------------------------------------------------------------------
CREATE TABLE payable (
    payable_id        TEXT    PRIMARY KEY
        CHECK (payable_id GLOB 'PAY-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    entity_id         TEXT    NOT NULL REFERENCES legal_entity (entity_id),
    bank_account_id   TEXT    NOT NULL REFERENCES bank_account (bank_account_id),
    payee_name        TEXT    NOT NULL,
    payable_category  TEXT    NOT NULL
        CHECK (payable_category IN ('TRADE', 'PAYROLL', 'TAX', 'DEBT_SERVICE', 'CAPEX', 'OTHER')),
    is_one_off        INTEGER NOT NULL DEFAULT 0 CHECK (is_one_off IN (0, 1)),
    document_ref      TEXT    NOT NULL,
    currency          TEXT    NOT NULL REFERENCES currency (currency_code),
    amount_minor      INTEGER NOT NULL CHECK (amount_minor > 0),
    invoice_date      TEXT    NOT NULL CHECK (date(invoice_date) = invoice_date),
    due_date          TEXT    NOT NULL CHECK (date(due_date) = due_date),
    expected_date     TEXT    NOT NULL CHECK (date(expected_date) = expected_date),
    status            TEXT    NOT NULL
        CHECK (status IN ('OPEN', 'PARTIALLY_SETTLED', 'OVERDUE', 'SETTLED', 'CANCELLED')),
    UNIQUE (entity_id, document_ref),
    CHECK (due_date >= invoice_date),
    CHECK (expected_date >= invoice_date),
    CHECK (substr(payable_id, 5, 4) = substr(invoice_date, 1, 4))
);
CREATE INDEX ix_payable_account ON payable (bank_account_id, expected_date);
CREATE INDEX ix_payable_entity  ON payable (entity_id);

-- -----------------------------------------------------------------------------
-- 6. LedgerEntry            (transaction, immutable)   LED-2026-00000001
--    Corrections are new REVERSAL rows, never edits. currency is stored on the
--    row (not only via the account) so a currency/account mismatch is observable.
-- -----------------------------------------------------------------------------
CREATE TABLE ledger_entry (
    ledger_entry_id            TEXT    PRIMARY KEY
        CHECK (ledger_entry_id GLOB 'LED-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    bank_account_id            TEXT    NOT NULL REFERENCES bank_account (bank_account_id),
    entry_date                 TEXT    NOT NULL CHECK (date(entry_date) = entry_date),
    direction                  TEXT    NOT NULL CHECK (direction IN ('CREDIT', 'DEBIT')),
    amount_minor               INTEGER NOT NULL CHECK (amount_minor > 0),
    currency                   TEXT    NOT NULL REFERENCES currency (currency_code),
    fx_rate_id                 TEXT    REFERENCES currency_rate (rate_id),
    base_amount_minor          INTEGER CHECK (base_amount_minor IS NULL OR base_amount_minor > 0),
    entry_type                 TEXT    NOT NULL
        CHECK (entry_type IN ('RECEIPT_SETTLEMENT', 'PAYMENT_SETTLEMENT', 'OPERATING_INFLOW',
                              'OPERATING_OUTFLOW', 'FEE_OR_INTEREST', 'REVERSAL')),
    external_ref               TEXT    NOT NULL,
    narrative                  TEXT,
    reverses_ledger_entry_id   TEXT    REFERENCES ledger_entry (ledger_entry_id),
    CHECK (substr(ledger_entry_id, 5, 4) = substr(entry_date, 1, 4)),
    CHECK ((entry_type = 'REVERSAL') = (reverses_ledger_entry_id IS NOT NULL)),
    CHECK (reverses_ledger_entry_id IS NULL OR reverses_ledger_entry_id <> ledger_entry_id),
    CHECK (entry_type NOT IN ('RECEIPT_SETTLEMENT', 'OPERATING_INFLOW') OR direction = 'CREDIT'),
    CHECK (entry_type NOT IN ('PAYMENT_SETTLEMENT', 'OPERATING_OUTFLOW') OR direction = 'DEBIT')
);
CREATE INDEX ix_ledger_account_date ON ledger_entry (bank_account_id, entry_date);
CREATE INDEX ix_ledger_dedupe       ON ledger_entry (bank_account_id, entry_date, direction, amount_minor);
CREATE INDEX ix_ledger_external_ref ON ledger_entry (external_ref);

-- -----------------------------------------------------------------------------
-- 7. SettlementLink         (bridge, N:M)
--    A receivable/payable can be settled by many ledger entries (partial
--    payments) and a ledger entry can settle many items (batch payment).
-- -----------------------------------------------------------------------------
CREATE TABLE settlement_link (
    settlement_link_id      INTEGER PRIMARY KEY,
    receivable_id           TEXT    REFERENCES receivable (receivable_id),
    payable_id              TEXT    REFERENCES payable (payable_id),
    ledger_entry_id         TEXT    NOT NULL REFERENCES ledger_entry (ledger_entry_id),
    allocated_amount_minor  INTEGER NOT NULL CHECK (allocated_amount_minor > 0),
    CHECK ((receivable_id IS NOT NULL) + (payable_id IS NOT NULL) = 1),
    UNIQUE (receivable_id, ledger_entry_id),
    UNIQUE (payable_id, ledger_entry_id)
);
CREATE INDEX ix_settlement_ledger ON settlement_link (ledger_entry_id);

-- -----------------------------------------------------------------------------
-- 8. Status event history   (append-only)
--    receivable.status / payable.status / forecast_run.status are denormalised
--    "current state" columns; the event rows are the source of truth.
-- -----------------------------------------------------------------------------
CREATE TABLE receivable_status_event (
    event_id         INTEGER PRIMARY KEY,
    receivable_id    TEXT    NOT NULL REFERENCES receivable (receivable_id),
    event_seq        INTEGER NOT NULL CHECK (event_seq >= 1),
    from_status      TEXT    CHECK (from_status IS NULL OR from_status IN
        ('OPEN', 'PARTIALLY_SETTLED', 'OVERDUE', 'SETTLED', 'WRITTEN_OFF', 'CANCELLED')),
    to_status        TEXT    NOT NULL CHECK (to_status IN
        ('OPEN', 'PARTIALLY_SETTLED', 'OVERDUE', 'SETTLED', 'WRITTEN_OFF', 'CANCELLED')),
    event_date       TEXT    NOT NULL CHECK (date(event_date) = event_date),
    cause            TEXT    NOT NULL CHECK (cause IN
        ('CREATED', 'PARTIAL_SETTLEMENT', 'FULL_SETTLEMENT', 'DUE_DATE_PASSED', 'WRITE_OFF', 'CANCELLATION')),
    ledger_entry_id  TEXT    REFERENCES ledger_entry (ledger_entry_id),
    actor_role       TEXT    NOT NULL CHECK (actor_role IN
        ('SYSTEM', 'CUSTOMER', 'OPERATIONS_ANALYST', 'RISK_COMPLIANCE_REVIEWER', 'SYSTEM_ADMINISTRATOR')),
    UNIQUE (receivable_id, event_seq),
    CHECK ((event_seq = 1) = (from_status IS NULL)),
    CHECK (from_status IS NULL OR from_status <> to_status)
);
CREATE INDEX ix_rcv_event_ledger ON receivable_status_event (ledger_entry_id);

CREATE TABLE payable_status_event (
    event_id         INTEGER PRIMARY KEY,
    payable_id       TEXT    NOT NULL REFERENCES payable (payable_id),
    event_seq        INTEGER NOT NULL CHECK (event_seq >= 1),
    from_status      TEXT    CHECK (from_status IS NULL OR from_status IN
        ('OPEN', 'PARTIALLY_SETTLED', 'OVERDUE', 'SETTLED', 'CANCELLED')),
    to_status        TEXT    NOT NULL CHECK (to_status IN
        ('OPEN', 'PARTIALLY_SETTLED', 'OVERDUE', 'SETTLED', 'CANCELLED')),
    event_date       TEXT    NOT NULL CHECK (date(event_date) = event_date),
    cause            TEXT    NOT NULL CHECK (cause IN
        ('CREATED', 'PARTIAL_SETTLEMENT', 'FULL_SETTLEMENT', 'DUE_DATE_PASSED', 'CANCELLATION')),
    ledger_entry_id  TEXT    REFERENCES ledger_entry (ledger_entry_id),
    actor_role       TEXT    NOT NULL CHECK (actor_role IN
        ('SYSTEM', 'CUSTOMER', 'OPERATIONS_ANALYST', 'RISK_COMPLIANCE_REVIEWER', 'SYSTEM_ADMINISTRATOR')),
    UNIQUE (payable_id, event_seq),
    CHECK ((event_seq = 1) = (from_status IS NULL)),
    CHECK (from_status IS NULL OR from_status <> to_status)
);
CREATE INDEX ix_pay_event_ledger ON payable_status_event (ledger_entry_id);

-- -----------------------------------------------------------------------------
-- 9. LiquidityScenario      (master, versioned)   SCN-001
--    Scenarios differ ONLY in the four numeric assumptions below.
-- -----------------------------------------------------------------------------
CREATE TABLE liquidity_scenario (
    scenario_id                  TEXT    PRIMARY KEY CHECK (scenario_id GLOB 'SCN-[0-9][0-9][0-9]'),
    scenario_code                TEXT    NOT NULL CHECK (scenario_code IN ('BASE', 'DELAYED_RECEIPT', 'STRESS')),
    version_no                   INTEGER NOT NULL DEFAULT 1 CHECK (version_no >= 1),
    scenario_name                TEXT    NOT NULL,
    assumption_notes             TEXT    NOT NULL,
    receipt_delay_days           INTEGER NOT NULL DEFAULT 0 CHECK (receipt_delay_days BETWEEN 0 AND 90),
    collection_haircut_bps       INTEGER NOT NULL DEFAULT 0 CHECK (collection_haircut_bps BETWEEN 0 AND 10000),
    fx_shock_bps                 INTEGER NOT NULL DEFAULT 0 CHECK (fx_shock_bps BETWEEN -5000 AND 5000),
    one_off_multiplier_bps       INTEGER NOT NULL DEFAULT 10000 CHECK (one_off_multiplier_bps BETWEEN 10000 AND 30000),
    status                       TEXT    NOT NULL CHECK (status IN ('DRAFT', 'ACTIVE', 'RETIRED')),
    effective_from               TEXT    NOT NULL CHECK (date(effective_from) = effective_from),
    effective_to                 TEXT    CHECK (effective_to IS NULL
                                                OR (date(effective_to) = effective_to
                                                    AND effective_to >= effective_from)),
    UNIQUE (scenario_code, version_no),
    -- BASE applies no adjustments.
    CHECK (scenario_code <> 'BASE'
           OR (receipt_delay_days = 0 AND collection_haircut_bps = 0
               AND fx_shock_bps = 0 AND one_off_multiplier_bps = 10000)),
    -- DELAYED_RECEIPT changes timing only.
    CHECK (scenario_code <> 'DELAYED_RECEIPT'
           OR (receipt_delay_days > 0 AND collection_haircut_bps = 0
               AND fx_shock_bps = 0 AND one_off_multiplier_bps = 10000))
);

-- -----------------------------------------------------------------------------
-- 10. ForecastRun           (versioned, immutable inputs)   FCAST-2026-01-01-001
--     ID = FCAST-<cutoff_date>-<run_seq>. A re-run for the same scenario and
--     cutoff is a NEW row (version_no + 1) pointing at the one it supersedes.
-- -----------------------------------------------------------------------------
CREATE TABLE forecast_run (
    forecast_run_id       TEXT    PRIMARY KEY,
    scenario_id           TEXT    NOT NULL REFERENCES liquidity_scenario (scenario_id),
    cutoff_date           TEXT    NOT NULL CHECK (date(cutoff_date) = cutoff_date),
    run_seq               INTEGER NOT NULL CHECK (run_seq BETWEEN 1 AND 999),
    version_no            INTEGER NOT NULL DEFAULT 1 CHECK (version_no >= 1),
    supersedes_run_id     TEXT    REFERENCES forecast_run (forecast_run_id),
    horizon_days          INTEGER NOT NULL CHECK (horizon_days BETWEEN 1 AND 365),
    horizon_end_date      TEXT    NOT NULL,
    base_currency         TEXT    NOT NULL REFERENCES currency (currency_code),
    status                TEXT    NOT NULL CHECK (status IN
        ('CREATED', 'RUNNING', 'COMPLETED', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'FAILED')),
    scenario_params_json  TEXT    NOT NULL CHECK (json_valid(scenario_params_json)),
    run_checksum          TEXT,
    UNIQUE (cutoff_date, scenario_id, version_no),
    CHECK (forecast_run_id = 'FCAST-' || cutoff_date || '-' || printf('%03d', run_seq)),
    CHECK (horizon_end_date = date(cutoff_date, '+' || horizon_days || ' days')),
    CHECK (supersedes_run_id IS NULL OR supersedes_run_id <> forecast_run_id),
    CHECK ((version_no = 1) = (supersedes_run_id IS NULL))
);
CREATE INDEX ix_forecast_run_cutoff ON forecast_run (cutoff_date);

CREATE TABLE forecast_run_status_event (
    event_id         INTEGER PRIMARY KEY,
    forecast_run_id  TEXT    NOT NULL REFERENCES forecast_run (forecast_run_id),
    event_seq        INTEGER NOT NULL CHECK (event_seq >= 1),
    from_status      TEXT    CHECK (from_status IS NULL OR from_status IN
        ('CREATED', 'RUNNING', 'COMPLETED', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'FAILED')),
    to_status        TEXT    NOT NULL CHECK (to_status IN
        ('CREATED', 'RUNNING', 'COMPLETED', 'APPROVED', 'REJECTED', 'SUPERSEDED', 'FAILED')),
    event_date       TEXT    NOT NULL CHECK (date(event_date) = event_date),
    cause            TEXT    NOT NULL CHECK (cause IN
        ('CREATED', 'STARTED', 'FINISHED', 'FAILED', 'REVIEW_APPROVED', 'REVIEW_REJECTED', 'SUPERSEDED')),
    actor_role       TEXT    NOT NULL CHECK (actor_role IN
        ('SYSTEM', 'CUSTOMER', 'OPERATIONS_ANALYST', 'RISK_COMPLIANCE_REVIEWER', 'SYSTEM_ADMINISTRATOR')),
    UNIQUE (forecast_run_id, event_seq),
    CHECK ((event_seq = 1) = (from_status IS NULL)),
    CHECK (from_status IS NULL OR from_status <> to_status)
);

-- -----------------------------------------------------------------------------
-- 11. CashFlowForecast      (item level, immutable)   CF-2026-000001
--     "Cash flow counted twice" and "actual used after cutoff" are NOT
--     constraints here (see header); they are detected via views / validators.
-- -----------------------------------------------------------------------------
CREATE TABLE cash_flow_forecast (
    cash_flow_forecast_id       TEXT    PRIMARY KEY
        CHECK (cash_flow_forecast_id GLOB 'CF-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    forecast_run_id             TEXT    NOT NULL REFERENCES forecast_run (forecast_run_id),
    bank_account_id             TEXT    NOT NULL REFERENCES bank_account (bank_account_id),
    source_type                 TEXT    NOT NULL CHECK (source_type IN ('RECEIVABLE', 'PAYABLE', 'RUN_RATE')),
    receivable_id               TEXT    REFERENCES receivable (receivable_id),
    payable_id                  TEXT    REFERENCES payable (payable_id),
    source_ledger_entry_id      TEXT    REFERENCES ledger_entry (ledger_entry_id),
    source_data_date            TEXT    NOT NULL CHECK (date(source_data_date) = source_data_date),
    direction                   TEXT    NOT NULL CHECK (direction IN ('CREDIT', 'DEBIT')),
    currency                    TEXT    NOT NULL REFERENCES currency (currency_code),
    expected_date               TEXT    NOT NULL CHECK (date(expected_date) = expected_date),
    forecast_date               TEXT    NOT NULL CHECK (date(forecast_date) = forecast_date),
    unadjusted_amount_minor     INTEGER NOT NULL CHECK (unadjusted_amount_minor > 0),
    forecast_amount_minor       INTEGER NOT NULL CHECK (forecast_amount_minor >= 0),
    fx_rate_id                  TEXT    REFERENCES currency_rate (rate_id),
    forecast_base_amount_minor  INTEGER CHECK (forecast_base_amount_minor IS NULL
                                               OR forecast_base_amount_minor >= 0),
    CHECK ((source_type = 'RECEIVABLE' AND receivable_id IS NOT NULL AND payable_id IS NULL)
        OR (source_type = 'PAYABLE'    AND payable_id    IS NOT NULL AND receivable_id IS NULL)
        OR (source_type = 'RUN_RATE'   AND receivable_id IS NULL     AND payable_id    IS NULL)),
    CHECK (source_type <> 'RECEIVABLE' OR direction = 'CREDIT'),
    CHECK (source_type <> 'PAYABLE'    OR direction = 'DEBIT'),
    CHECK (forecast_date >= expected_date)
);
CREATE INDEX ix_cff_run        ON cash_flow_forecast (forecast_run_id, forecast_date);
CREATE INDEX ix_cff_receivable ON cash_flow_forecast (receivable_id);
CREATE INDEX ix_cff_payable    ON cash_flow_forecast (payable_id);
CREATE INDEX ix_cff_account    ON cash_flow_forecast (bank_account_id);

-- -----------------------------------------------------------------------------
-- 12. DailyCashPosition     (derived from ledger_entry + bank_account)
--     One row per account per calendar day from opening_date. closing_minor is
--     stored (not a generated column) so a reconciliation break can exist and
--     be labelled; v_daily_position_arithmetic_breaks finds it.
-- -----------------------------------------------------------------------------
CREATE TABLE daily_cash_position (
    bank_account_id      TEXT    NOT NULL REFERENCES bank_account (bank_account_id),
    position_date        TEXT    NOT NULL CHECK (date(position_date) = position_date),
    currency             TEXT    NOT NULL REFERENCES currency (currency_code),
    opening_minor        INTEGER NOT NULL,
    total_credits_minor  INTEGER NOT NULL CHECK (total_credits_minor >= 0),
    total_debits_minor   INTEGER NOT NULL CHECK (total_debits_minor >= 0),
    closing_minor        INTEGER NOT NULL,
    entry_count          INTEGER NOT NULL CHECK (entry_count >= 0),
    fx_rate_id           TEXT    REFERENCES currency_rate (rate_id),
    closing_base_minor   INTEGER,
    PRIMARY KEY (bank_account_id, position_date)
);
CREATE INDEX ix_dcp_date ON daily_cash_position (position_date);

-- -----------------------------------------------------------------------------
-- 13. ForecastDailyProjection  (derived: projected liquidity per run, entity, day)
-- -----------------------------------------------------------------------------
CREATE TABLE forecast_daily_projection (
    forecast_run_id                TEXT    NOT NULL REFERENCES forecast_run (forecast_run_id),
    entity_id                      TEXT    NOT NULL REFERENCES legal_entity (entity_id),
    projection_date                TEXT    NOT NULL CHECK (date(projection_date) = projection_date),
    opening_base_minor             INTEGER NOT NULL,
    projected_inflow_base_minor    INTEGER NOT NULL CHECK (projected_inflow_base_minor >= 0),
    projected_outflow_base_minor   INTEGER NOT NULL CHECK (projected_outflow_base_minor >= 0),
    projected_closing_base_minor   INTEGER NOT NULL,
    liquidity_buffer_base_minor    INTEGER NOT NULL CHECK (liquidity_buffer_base_minor >= 0),
    shortfall_flag                 INTEGER NOT NULL CHECK (shortfall_flag IN (0, 1)),
    PRIMARY KEY (forecast_run_id, entity_id, projection_date),
    CHECK (projected_closing_base_minor =
           opening_base_minor + projected_inflow_base_minor - projected_outflow_base_minor),
    CHECK (shortfall_flag = (projected_closing_base_minor < liquidity_buffer_base_minor))
);

-- -----------------------------------------------------------------------------
-- 14. ForecastVsActual      (comparison, one row per item per as-of date)   VAR-2026-000001
--     Signed base amounts: inflow positive, outflow negative.
--     variance = actual - forecast (positive = better cash than forecast).
--     fx_variance is the residual after the amount effect, so the split always
--     reconciles by construction.
-- -----------------------------------------------------------------------------
CREATE TABLE forecast_vs_actual (
    comparison_id               TEXT    PRIMARY KEY
        CHECK (comparison_id GLOB 'VAR-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]'),
    forecast_run_id             TEXT    NOT NULL REFERENCES forecast_run (forecast_run_id),
    cash_flow_forecast_id       TEXT    REFERENCES cash_flow_forecast (cash_flow_forecast_id),
    actual_ledger_entry_id      TEXT    REFERENCES ledger_entry (ledger_entry_id),
    compared_through_date       TEXT    NOT NULL CHECK (date(compared_through_date) = compared_through_date),
    direction                   TEXT    NOT NULL CHECK (direction IN ('CREDIT', 'DEBIT')),
    currency                    TEXT    NOT NULL REFERENCES currency (currency_code),
    forecast_date               TEXT    CHECK (forecast_date IS NULL OR date(forecast_date) = forecast_date),
    actual_date                 TEXT    CHECK (actual_date IS NULL OR date(actual_date) = actual_date),
    date_variance_days          INTEGER,
    forecast_amount_minor       INTEGER NOT NULL CHECK (forecast_amount_minor >= 0),
    actual_amount_minor         INTEGER NOT NULL CHECK (actual_amount_minor >= 0),
    forecast_net_base_minor     INTEGER NOT NULL,
    actual_net_base_minor       INTEGER NOT NULL,
    variance_base_minor         INTEGER NOT NULL,
    amount_variance_base_minor  INTEGER NOT NULL,
    fx_variance_base_minor      INTEGER NOT NULL,
    variance_driver             TEXT    NOT NULL CHECK (variance_driver IN
        ('ON_TARGET', 'TIMING_LATE', 'TIMING_EARLY', 'PARTIAL_OR_SHORT_PAYMENT', 'FX_MOVEMENT',
         'UNFORECAST_ONE_OFF', 'UNFORECAST_OTHER', 'NOT_YET_SETTLED',
         'SCENARIO_AMOUNT_ADJUSTMENT', 'RUN_RATE_DEVIATION')),
    UNIQUE (cash_flow_forecast_id, compared_through_date),
    CHECK (cash_flow_forecast_id IS NOT NULL OR actual_ledger_entry_id IS NOT NULL),
    CHECK (variance_base_minor = actual_net_base_minor - forecast_net_base_minor),
    CHECK (variance_base_minor = amount_variance_base_minor + fx_variance_base_minor)
);
CREATE INDEX ix_fva_run    ON forecast_vs_actual (forecast_run_id);
CREATE INDEX ix_fva_driver ON forecast_vs_actual (variance_driver);

-- =============================================================================
-- Append-only protection (history is never overwritten)
-- =============================================================================
CREATE TRIGGER trg_ledger_entry_no_update BEFORE UPDATE ON ledger_entry
BEGIN SELECT RAISE(ABORT, 'ledger_entry is append-only: post a REVERSAL entry instead'); END;
CREATE TRIGGER trg_ledger_entry_no_delete BEFORE DELETE ON ledger_entry
BEGIN SELECT RAISE(ABORT, 'ledger_entry is append-only: post a REVERSAL entry instead'); END;

CREATE TRIGGER trg_cash_flow_forecast_no_update BEFORE UPDATE ON cash_flow_forecast
BEGIN SELECT RAISE(ABORT, 'cash_flow_forecast is immutable: create a new forecast_run version'); END;
CREATE TRIGGER trg_cash_flow_forecast_no_delete BEFORE DELETE ON cash_flow_forecast
BEGIN SELECT RAISE(ABORT, 'cash_flow_forecast is immutable: create a new forecast_run version'); END;

CREATE TRIGGER trg_receivable_event_no_update BEFORE UPDATE ON receivable_status_event
BEGIN SELECT RAISE(ABORT, 'status events are append-only'); END;
CREATE TRIGGER trg_receivable_event_no_delete BEFORE DELETE ON receivable_status_event
BEGIN SELECT RAISE(ABORT, 'status events are append-only'); END;

CREATE TRIGGER trg_payable_event_no_update BEFORE UPDATE ON payable_status_event
BEGIN SELECT RAISE(ABORT, 'status events are append-only'); END;
CREATE TRIGGER trg_payable_event_no_delete BEFORE DELETE ON payable_status_event
BEGIN SELECT RAISE(ABORT, 'status events are append-only'); END;

CREATE TRIGGER trg_forecast_run_event_no_update BEFORE UPDATE ON forecast_run_status_event
BEGIN SELECT RAISE(ABORT, 'status events are append-only'); END;
CREATE TRIGGER trg_forecast_run_event_no_delete BEFORE DELETE ON forecast_run_status_event
BEGIN SELECT RAISE(ABORT, 'status events are append-only'); END;

-- =============================================================================
-- Validation views (join validation and reconciliation; one row = one problem,
-- except v_*_settlement and v_daily_entity_position which are reporting views).
-- They read only input tables, never ground truth.
-- =============================================================================

-- Settled / outstanding per receivable. outstanding_minor < 0 means over-settled
-- (e.g. a cash flow counted twice).
CREATE VIEW v_receivable_settlement AS
SELECT r.receivable_id,
       r.entity_id,
       r.currency,
       r.amount_minor,
       r.expected_date,
       r.due_date,
       r.status,
       COALESCE(SUM(sl.allocated_amount_minor), 0)                      AS settled_minor,
       r.amount_minor - COALESCE(SUM(sl.allocated_amount_minor), 0)     AS outstanding_minor,
       MIN(le.entry_date)                                               AS first_settlement_date,
       MAX(le.entry_date)                                               AS last_settlement_date,
       CAST(julianday(MAX(le.entry_date)) - julianday(r.expected_date) AS INTEGER)
                                                                        AS days_after_expected
FROM receivable r
LEFT JOIN settlement_link sl ON sl.receivable_id = r.receivable_id
LEFT JOIN ledger_entry   le ON le.ledger_entry_id = sl.ledger_entry_id
GROUP BY r.receivable_id;

CREATE VIEW v_payable_settlement AS
SELECT p.payable_id,
       p.entity_id,
       p.currency,
       p.amount_minor,
       p.expected_date,
       p.due_date,
       p.status,
       COALESCE(SUM(sl.allocated_amount_minor), 0)                      AS settled_minor,
       p.amount_minor - COALESCE(SUM(sl.allocated_amount_minor), 0)     AS outstanding_minor,
       MIN(le.entry_date)                                               AS first_settlement_date,
       MAX(le.entry_date)                                               AS last_settlement_date,
       CAST(julianday(MAX(le.entry_date)) - julianday(p.expected_date) AS INTEGER)
                                                                        AS days_after_expected
FROM payable p
LEFT JOIN settlement_link sl ON sl.payable_id = p.payable_id
LEFT JOIN ledger_entry   le ON le.ledger_entry_id = sl.ledger_entry_id
GROUP BY p.payable_id;

-- Day does not add up: closing != opening + credits - debits.
CREATE VIEW v_daily_position_arithmetic_breaks AS
SELECT bank_account_id,
       position_date,
       opening_minor,
       total_credits_minor,
       total_debits_minor,
       closing_minor,
       opening_minor + total_credits_minor - total_debits_minor AS expected_closing_minor,
       closing_minor - (opening_minor + total_credits_minor - total_debits_minor) AS diff_minor
FROM daily_cash_position
WHERE closing_minor <> opening_minor + total_credits_minor - total_debits_minor;

-- Day N opening != day N-1 closing (consecutive daily rows per account).
CREATE VIEW v_daily_position_chain_breaks AS
SELECT bank_account_id,
       position_date,
       prev_position_date,
       prev_closing_minor,
       opening_minor,
       opening_minor - prev_closing_minor AS diff_minor
FROM (
    SELECT bank_account_id,
           position_date,
           opening_minor,
           LAG(position_date)  OVER w AS prev_position_date,
           LAG(closing_minor)  OVER w AS prev_closing_minor
    FROM daily_cash_position
    WINDOW w AS (PARTITION BY bank_account_id ORDER BY position_date)
)
WHERE prev_closing_minor IS NOT NULL
  AND opening_minor <> prev_closing_minor;

-- Stored closing != account opening balance + cumulative signed ledger.
-- Assumes position rows exist for consecutive days; ledger rows on dates with
-- no position row are not counted (checked separately by src/validate).
CREATE VIEW v_position_vs_ledger_breaks AS
SELECT bank_account_id,
       position_date,
       closing_minor,
       opening_balance_minor + cumulative_net_minor AS expected_closing_minor,
       closing_minor - (opening_balance_minor + cumulative_net_minor) AS diff_minor
FROM (
    SELECT p.bank_account_id,
           p.position_date,
           p.closing_minor,
           b.opening_balance_minor,
           SUM(COALESCE(n.net_minor, 0)) OVER (
               PARTITION BY p.bank_account_id
               ORDER BY p.position_date
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           ) AS cumulative_net_minor
    FROM daily_cash_position p
    JOIN bank_account b ON b.bank_account_id = p.bank_account_id
    LEFT JOIN (
        SELECT bank_account_id,
               entry_date,
               SUM(CASE direction WHEN 'CREDIT' THEN amount_minor ELSE -amount_minor END) AS net_minor
        FROM ledger_entry
        GROUP BY bank_account_id, entry_date
    ) n ON n.bank_account_id = p.bank_account_id
       AND n.entry_date      = p.position_date
)
WHERE closing_minor <> opening_balance_minor + cumulative_net_minor;

-- Consolidated daily position per legal entity, in base currency.
-- accounts_missing_fx > 0 means the total is understated and must not be trusted.
CREATE VIEW v_daily_entity_position AS
SELECT b.entity_id,
       p.position_date,
       SUM(p.closing_base_minor)            AS closing_base_minor,
       COUNT(*)                             AS account_count,
       SUM(p.closing_base_minor IS NULL)    AS accounts_missing_fx
FROM daily_cash_position p
JOIN bank_account b ON b.bank_account_id = p.bank_account_id
GROUP BY b.entity_id, p.position_date;

-- Forecast items that consumed information from after the run's cutoff.
CREATE VIEW v_forecast_cutoff_violations AS
SELECT cf.cash_flow_forecast_id,
       cf.forecast_run_id,
       fr.cutoff_date,
       cf.source_data_date,
       cf.source_ledger_entry_id,
       le.entry_date AS source_ledger_entry_date
FROM cash_flow_forecast cf
JOIN forecast_run fr         ON fr.forecast_run_id = cf.forecast_run_id
LEFT JOIN ledger_entry le    ON le.ledger_entry_id = cf.source_ledger_entry_id
WHERE cf.source_data_date > fr.cutoff_date
   OR le.entry_date       > fr.cutoff_date;

-- Currency problems on rows that should have been converted.
--   CURRENCY_MISMATCH       : entry/item currency differs from its bank account.
--   UNCONVERTED_BASE_AMOUNT : non-base currency, base amount present, but it equals
--                             the local amount or no rate is attached.
--   FX_RATE_MISSING         : non-base currency with neither a rate nor a base amount.
CREATE VIEW v_currency_integrity_issues AS
SELECT 'ledger_entry'           AS table_name,
       le.ledger_entry_id       AS record_id,
       'CURRENCY_MISMATCH'      AS issue,
       le.currency              AS row_currency,
       ba.currency              AS account_currency
FROM ledger_entry le
JOIN bank_account ba ON ba.bank_account_id = le.bank_account_id
WHERE le.currency <> ba.currency
UNION ALL
SELECT 'cash_flow_forecast', cf.cash_flow_forecast_id, 'CURRENCY_MISMATCH', cf.currency, ba.currency
FROM cash_flow_forecast cf
JOIN bank_account ba ON ba.bank_account_id = cf.bank_account_id
WHERE cf.currency <> ba.currency
UNION ALL
SELECT 'ledger_entry', le.ledger_entry_id, 'UNCONVERTED_BASE_AMOUNT', le.currency, NULL
FROM ledger_entry le
JOIN currency c ON c.currency_code = le.currency
WHERE c.is_base = 0
  AND le.base_amount_minor IS NOT NULL
  AND (le.base_amount_minor = le.amount_minor OR le.fx_rate_id IS NULL)
UNION ALL
SELECT 'ledger_entry', le.ledger_entry_id, 'FX_RATE_MISSING', le.currency, NULL
FROM ledger_entry le
JOIN currency c ON c.currency_code = le.currency
WHERE c.is_base = 0
  AND le.fx_rate_id IS NULL
  AND le.base_amount_minor IS NULL;
