"""Validation checks. Each check reads tables only (never ground truth) and returns the records that break a rule.

Rules come from three places: the schema itself (keys, foreign keys, NOT NULL, enums are read
from sql/schema.sql and applied to the DataFrames, so data that could not be loaded is still
reported), the ID formats in src/ids.py, and business rules written here (dates, arithmetic,
lifecycle, duplicates, currency). Business rules are NOT schema constraints, so injected
anomalies load and are caught here and in the SQL views.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from ..db import create_database
from ..ids import ID_PATTERNS
from .reconcile import reconcile

CATEGORIES = ("PK_UNIQUENESS", "FK_INTEGRITY", "ALLOWED_ENUMS", "ID_FORMAT", "DATE_ORDERING",
              "ARITHMETIC_RECONCILIATION", "LIFECYCLE_TRANSITIONS", "DUPLICATES", "CURRENCY_CONSISTENCY")
VIOLATION_COLUMNS = ["check_id", "category", "table_name", "record_id", "detail"]

# ---------------------------------------------------------------------------------------------
# lifecycle contract (docs/data_dictionary.md). Restated here on purpose: the validator must not
# import the generator's own tables, or a generator bug would validate itself.
RECEIVABLE_TRANSITIONS = {
    None: {"OPEN"},
    "OPEN": {"PARTIALLY_SETTLED", "OVERDUE", "SETTLED", "CANCELLED"},
    "PARTIALLY_SETTLED": {"OVERDUE", "SETTLED", "WRITTEN_OFF"},
    "OVERDUE": {"PARTIALLY_SETTLED", "SETTLED", "WRITTEN_OFF"},
}
PAYABLE_TRANSITIONS = {
    None: {"OPEN"},
    "OPEN": {"PARTIALLY_SETTLED", "OVERDUE", "SETTLED", "CANCELLED"},
    "PARTIALLY_SETTLED": {"OVERDUE", "SETTLED"},
    "OVERDUE": {"PARTIALLY_SETTLED", "SETTLED"},
}
RUN_TRANSITIONS = {
    None: {"CREATED"}, "CREATED": {"RUNNING"}, "RUNNING": {"COMPLETED", "FAILED"},
    "COMPLETED": {"APPROVED", "REJECTED", "SUPERSEDED"}, "APPROVED": {"SUPERSEDED"}, "REJECTED": {"SUPERSEDED"},
}
CAUSE_FOR_STATUS = {"OPEN": {"CREATED"}, "PARTIALLY_SETTLED": {"PARTIAL_SETTLEMENT"}, "SETTLED": {"FULL_SETTLEMENT"},
                    "OVERDUE": {"DUE_DATE_PASSED"}, "WRITTEN_OFF": {"WRITE_OFF"}, "CANCELLED": {"CANCELLATION"}}
RUN_CAUSE_FOR_STATUS = {"CREATED": {"CREATED"}, "RUNNING": {"STARTED"}, "COMPLETED": {"FINISHED"}, "FAILED": {"FAILED"},
                        "APPROVED": {"REVIEW_APPROVED"}, "REJECTED": {"REVIEW_REJECTED"}, "SUPERSEDED": {"SUPERSEDED"}}

# (table, id column, id kind in ids.ID_PATTERNS). Foreign-key columns are checked against the kind of the parent.
ID_COLUMNS = [
    ("legal_entity", "entity_id", "entity"), ("legal_entity", "parent_entity_id", "entity"),
    ("bank_account", "bank_account_id", "bank_account"), ("bank_account", "entity_id", "entity"),
    ("currency_rate", "rate_id", "currency_rate"),
    ("receivable", "receivable_id", "receivable"), ("receivable", "entity_id", "entity"),
    ("receivable", "bank_account_id", "bank_account"),
    ("payable", "payable_id", "payable"), ("payable", "entity_id", "entity"),
    ("payable", "bank_account_id", "bank_account"),
    ("ledger_entry", "ledger_entry_id", "ledger_entry"), ("ledger_entry", "bank_account_id", "bank_account"),
    ("ledger_entry", "fx_rate_id", "currency_rate"), ("ledger_entry", "reverses_ledger_entry_id", "ledger_entry"),
    ("liquidity_scenario", "scenario_id", "scenario"),
    ("forecast_run", "forecast_run_id", "forecast_run"), ("forecast_run", "scenario_id", "scenario"),
    ("forecast_run", "supersedes_run_id", "forecast_run"),
    ("cash_flow_forecast", "cash_flow_forecast_id", "cash_flow"),
    ("cash_flow_forecast", "forecast_run_id", "forecast_run"),
    ("cash_flow_forecast", "bank_account_id", "bank_account"),
    ("cash_flow_forecast", "source_ledger_entry_id", "ledger_entry"),
    ("cash_flow_forecast", "fx_rate_id", "currency_rate"),
    ("daily_cash_position", "bank_account_id", "bank_account"), ("daily_cash_position", "fx_rate_id", "currency_rate"),
    ("forecast_vs_actual", "comparison_id", "comparison"), ("forecast_vs_actual", "forecast_run_id", "forecast_run"),
    ("forecast_vs_actual", "cash_flow_forecast_id", "cash_flow"),
    ("forecast_vs_actual", "actual_ledger_entry_id", "ledger_entry"),
]


# ---------------------------------------------------------------------------------------------
# schema metadata, read from the schema file itself
class SchemaInfo:
    def __init__(self) -> None:
        con = create_database()
        self.tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        self.columns, self.pk, self.fks, self.unique, self.enums, self.not_null = {}, {}, {}, {}, {}, {}
        for t in self.tables:
            info = con.execute(f"PRAGMA table_info({t})").fetchall()
            self.columns[t] = [r[1] for r in info]
            self.pk[t] = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
            self.not_null[t] = [r[1] for r in info if r[3] and not r[5]]
            self.fks[t] = [(r[3], r[2], r[4]) for r in con.execute(f"PRAGMA foreign_key_list({t})")]
            uniq = []
            for idx in con.execute(f"PRAGMA index_list({t})"):
                if idx[2] and idx[3] == "u":
                    uniq.append([c[2] for c in con.execute(f"PRAGMA index_info({idx[1]})")])
            self.unique[t] = uniq
            sql = con.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0]
            enums = {}
            for m in re.finditer(r"(\w+)\s+IN\s*\(\s*((?:'[^']*'|\d+)(?:\s*,\s*(?:'[^']*'|\d+))*)\s*\)", sql):
                col, vals = m.group(1), m.group(2)
                if col == "NOT":
                    continue
                enums.setdefault(col, set()).update(v.strip().strip("'") for v in vals.split(","))
            self.enums[t] = enums
        con.close()


_SCHEMA: SchemaInfo | None = None


def schema() -> SchemaInfo:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = SchemaInfo()
    return _SCHEMA


# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    check_id: str
    category: str
    description: str
    fn: Callable[[dict], tuple[int, pd.DataFrame]]


REGISTRY: list[Check] = []


def check(check_id: str, category: str, description: str):
    assert category in CATEGORIES

    def deco(fn):
        REGISTRY.append(Check(check_id, category, description, fn))
        return fn
    return deco


def _bad(table: str, ids, detail) -> pd.DataFrame:
    ids = pd.Series(ids).astype(str).reset_index(drop=True)
    detail = pd.Series(detail, index=ids.index) if not isinstance(detail, pd.Series) else detail.reset_index(drop=True)
    return pd.DataFrame({"table_name": table, "record_id": ids, "detail": detail.astype(str)})


def _by(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Lookup table by key. Duplicated keys keep the first row, so a check can still run (and the
    duplicate is reported by PK_UNIQUE) instead of crashing on bad data."""
    return df.drop_duplicates(key).set_index(key)


def _none() -> pd.DataFrame:
    return pd.DataFrame(columns=["table_name", "record_id", "detail"])


def _cat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if len(p)]
    return pd.concat(parts, ignore_index=True) if parts else _none()


def _key(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    if df.empty:
        return pd.Series([], dtype=object, index=df.index)
    return df[cols].astype(object).where(df[cols].notna(), "<null>").astype(str).agg("|".join, axis=1)


def _norm(s: pd.Series) -> pd.Series:
    """Values as comparable text: 1.0 -> '1', NaN stays NaN."""
    def one(v):
        if isinstance(v, (float, np.floating)) and float(v).is_integer():
            return str(int(v))
        return str(v)
    return s.map(lambda v: v if pd.isna(v) else one(v))


def _tables(t: dict) -> list[str]:
    return [n for n in schema().tables if n in t]


# ---- 1. primary keys ---------------------------------------------------------------------------------
@check("PK_UNIQUE", "PK_UNIQUENESS", "Primary key and UNIQUE constraints hold on every table")
def pk_unique(t):
    n, parts = 0, []
    for name in _tables(t):
        df = t[name]
        for cols, kind in [(schema().pk[name], "primary key")] + [(u, "unique key") for u in schema().unique[name]]:
            if not cols:
                continue
            n += len(df)
            usable = df[cols].notna().all(axis=1) if kind == "unique key" else pd.Series(True, index=df.index)
            key = _key(df[usable], cols)
            dup = key[key.duplicated(keep=False)]
            parts.append(_bad(name, dup.values, f"{kind} ({', '.join(cols)}) repeated"))
    return n, _cat(parts)


@check("NOT_NULL", "PK_UNIQUENESS", "Columns declared NOT NULL are never empty, primary key parts included")
def not_null(t):
    n, parts = 0, []
    for name in _tables(t):
        df = t[name]
        ident = schema().pk[name][0] if schema().pk[name] else df.columns[0]
        for col in schema().not_null[name] + schema().pk[name]:
            if col not in df:
                parts.append(_bad(name, [col], f"column {col} missing"))
                continue
            n += len(df)
            miss = df[df[col].isna()]
            parts.append(_bad(name, _key(miss, [ident]).values, f"{col} is empty"))
    return n, _cat(parts)


# ---- 2. foreign keys ----------------------------------------------------------------------------------
@check("FK_EXISTS", "FK_INTEGRITY", "Every non-null foreign key points at an existing parent row")
def fk_exists(t):
    n, parts = 0, []
    for name in _tables(t):
        df = t[name]
        ident = schema().pk[name]
        for col, parent, pcol in schema().fks[name]:
            if parent not in t:
                continue
            pcol = pcol or schema().pk[parent][0]
            vals = df[col].dropna()
            n += len(vals)
            missing = vals[~vals.isin(set(t[parent][pcol].dropna()))]
            rid = _key(df.loc[missing.index], ident) if ident else pd.Series(missing.index.astype(str))
            parts.append(_bad(name, rid.values, f"{col} = {missing.values} not found in {parent}.{pcol}"
                              if False else pd.Series([f"{col} {v} not found in {parent}.{pcol}" for v in missing.values])))
    return n, _cat(parts)


@check("FK_SETTLEMENT_XOR", "FK_INTEGRITY", "A settlement link points at exactly one of receivable / payable")
def settlement_xor(t):
    sl = t["settlement_link"]
    bad = sl[sl.receivable_id.notna() == sl.payable_id.notna()]
    return len(sl), _bad("settlement_link", bad.settlement_link_id, "needs exactly one of receivable_id / payable_id")


# ---- 3. enums ------------------------------------------------------------------------------------------
@check("ENUM_VALUES", "ALLOWED_ENUMS", "Every constrained column holds only values the schema allows")
def enum_values(t):
    n, parts = 0, []
    for name in _tables(t):
        df = t[name]
        ident = schema().pk[name] or [df.columns[0]]
        for col, allowed in schema().enums[name].items():
            if col not in df:
                continue
            vals = df[col].dropna()
            n += len(vals)
            bad = vals[~_norm(vals).isin(allowed)]
            if len(bad):
                parts.append(_bad(name, _key(df.loc[bad.index], ident).values,
                                  pd.Series([f"{col} = {v!r} not in {sorted(allowed)}" for v in bad.values])))
    return n, _cat(parts)


# ---- 4. ID formats and ID consistency --------------------------------------------------------------------
@check("ID_REGEX", "ID_FORMAT", "Every identifier (and every foreign key to one) matches its documented format")
def id_regex(t):
    n, parts = 0, []
    for table, col, kind in ID_COLUMNS:
        if table not in t:
            continue
        df = t[table]
        vals = df[col].dropna().astype(str)
        n += len(vals)
        bad = vals[~vals.map(lambda v: bool(ID_PATTERNS[kind].fullmatch(v)))]
        parts.append(_bad(table, bad.values, pd.Series([f"{col} {v!r} is not a valid {kind} id" for v in bad.values])))
    return n, _cat(parts)


def _year_of_date(s: pd.Series) -> pd.Series:
    return s.astype(str).str[:4]


@check("ID_CONSISTENT", "ID_FORMAT", "The parts of an ID agree with the row: entity in account id, year in ledger/document ids, rate id with currency and date")
def id_consistent(t):
    n, parts = 0, []

    def add(table, idcol, ok, df, why):
        nonlocal n
        n += len(df)
        parts.append(_bad(table, df.loc[~ok, idcol].values, why))

    ba = t["bank_account"]
    add("bank_account", "bank_account_id", ba.bank_account_id.str.slice(8, 18) == ba.entity_id, ba,
        "account id does not embed its entity id")
    add("bank_account", "bank_account_id", ba.bank_account_id.str[-3:].astype(int) == ba.account_seq, ba,
        "account id counter differs from account_seq")
    le = t["ledger_entry"]
    add("ledger_entry", "ledger_entry_id", le.ledger_entry_id.str.slice(4, 8) == _year_of_date(le.entry_date), le,
        "ledger id year differs from entry_date year")
    for tbl, idc in (("receivable", "receivable_id"), ("payable", "payable_id")):
        d = t[tbl]
        add(tbl, idc, d[idc].str.slice(4, 8) == _year_of_date(d.invoice_date), d, "id year differs from invoice_date year")
    fr = t["forecast_run"]
    add("forecast_run", "forecast_run_id",
        fr.forecast_run_id == "FCAST-" + fr.cutoff_date + "-" + fr.run_seq.astype(int).astype(str).str.zfill(3), fr,
        "run id differs from cutoff date and sequence")
    cf = t["cash_flow_forecast"].merge(fr[["forecast_run_id", "cutoff_date"]], on="forecast_run_id", how="left")
    add("cash_flow_forecast", "cash_flow_forecast_id",
        cf.cash_flow_forecast_id.str.slice(3, 7) == _year_of_date(cf.cutoff_date), cf, "forecast id year differs from run cutoff year")
    r = t["currency_rate"]
    add("currency_rate", "rate_id", r.rate_id == "FXR-" + r.quote_currency + "-" + r.rate_date.str.replace("-", ""), r,
        "rate id differs from currency and date")
    return n, _cat(parts)


# ---- 5. date ordering -----------------------------------------------------------------------------------
def _is_iso(s: pd.Series) -> pd.Series:
    ok = s.astype(str).str.fullmatch(r"\d{4}-\d{2}-\d{2}")
    parsed = pd.to_datetime(s.where(ok), format="%Y-%m-%d", errors="coerce")
    return ok & parsed.notna()


DATE_COLUMNS = {
    "legal_entity": ["effective_from", "effective_to", "suspended_from"], "bank_account": ["opening_date", "effective_from", "effective_to"],
    "currency_rate": ["rate_date"], "receivable": ["invoice_date", "due_date", "expected_date"],
    "payable": ["invoice_date", "due_date", "expected_date"], "ledger_entry": ["entry_date"],
    "receivable_status_event": ["event_date"], "payable_status_event": ["event_date"],
    "forecast_run_status_event": ["event_date"], "liquidity_scenario": ["effective_from", "effective_to"],
    "forecast_run": ["cutoff_date", "horizon_end_date"],
    "cash_flow_forecast": ["source_data_date", "expected_date", "forecast_date"],
    "daily_cash_position": ["position_date"],
    "forecast_vs_actual": ["compared_through_date", "forecast_date", "actual_date"],
}


@check("DATE_FORMAT", "DATE_ORDERING", "Every date column holds a real YYYY-MM-DD date")
def date_format(t):
    n, parts = 0, []
    for name, cols in DATE_COLUMNS.items():
        if name not in t:
            continue
        df = t[name]
        ident = schema().pk[name] or [df.columns[0]]
        for c in cols:
            vals = df[c].dropna()
            n += len(vals)
            bad = vals[~_is_iso(vals)]
            if len(bad):
                parts.append(_bad(name, _key(df.loc[bad.index], ident).values,
                                  pd.Series([f"{c} {v!r} is not a date" for v in bad.values])))
    return n, _cat(parts)


def _pair(t, table, early, late, why, strict=False):
    df = t[table]
    ident = schema().pk[table] or [df.columns[0]]
    both = df[df[early].notna() & df[late].notna()]
    bad = both[(both[early] >= both[late]) if strict else (both[early] > both[late])]
    return len(both), _bad(table, _key(bad, ident).values, why)


@check("DATE_PAIRS", "DATE_ORDERING", "Within a row: invoice <= due and expected, effective_from <= effective_to, cutoff < horizon end, source data <= cutoff... see detail")
def date_pairs(t):
    n, parts = 0, []
    for table in ("receivable", "payable"):
        for e, l in (("invoice_date", "due_date"), ("invoice_date", "expected_date")):
            a, b = _pair(t, table, e, l, f"{e} after {l}")
            n += a
            parts.append(b)
    for table in ("legal_entity", "bank_account", "liquidity_scenario"):
        a, b = _pair(t, table, "effective_from", "effective_to", "effective_from after effective_to")
        n += a
        parts.append(b)
    a, b = _pair(t, "legal_entity", "effective_from", "suspended_from", "suspended before it began", strict=True)
    n += a
    parts.append(b)
    a, b = _pair(t, "forecast_run", "cutoff_date", "horizon_end_date", "cutoff not before horizon end", strict=True)
    n += a
    parts.append(b)
    a, b = _pair(t, "cash_flow_forecast", "expected_date", "forecast_date", "forecast_date earlier than expected_date")
    n += a
    parts.append(b)
    return n, _cat(parts)


@check("DATE_CUTOFF", "DATE_ORDERING", "A forecast item uses no data dated after its run's cutoff (source data, source ledger entry, FX rate)")
def date_cutoff(t):
    cf = t["cash_flow_forecast"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id", how="left")
    led = _by(t["ledger_entry"], "ledger_entry_id").entry_date
    rate = _by(t["currency_rate"], "rate_id").rate_date
    src = cf.source_ledger_entry_id.map(led)
    fxd = cf.fx_rate_id.map(rate)
    late = (cf.source_data_date > cf.cutoff_date) | (src > cf.cutoff_date) | (fxd > cf.cutoff_date)
    bad = cf[late.fillna(False)]
    why = pd.Series([f"source_data_date {a}, source entry date {b}, fx date {c}, cutoff {d}" for a, b, c, d in
                     zip(bad.source_data_date, src[bad.index], fxd[bad.index], bad.cutoff_date)])
    return len(cf), _bad("cash_flow_forecast", bad.cash_flow_forecast_id.values, why)


@check("DATE_EVENTS", "DATE_ORDERING", "Status events run in date order, start on the document's invoice date and end on a date that matches the stored status")
def date_events(t):
    n, parts = 0, []
    for table, parent, pid, ptable in (("receivable_status_event", "receivable", "receivable_id", "receivable"),
                                       ("payable_status_event", "payable", "payable_id", "payable"),
                                       ("forecast_run_status_event", "forecast_run", "forecast_run_id", "forecast_run")):
        ev = t[table].sort_values([pid, "event_seq"], kind="stable")
        n += len(ev)
        prev = ev.groupby(pid).event_date.shift(1)
        bad = ev[prev.notna() & (ev.event_date < prev)]
        parts.append(_bad(table, bad.event_id.values, "event_date earlier than the previous event"))
        if table != "forecast_run_status_event":
            first = _by(ev[ev.event_seq == 1], pid).event_date
            inv = _by(t[ptable], pid).invoice_date.reindex(first.index)
            off = first[first != inv]
            parts.append(_bad(table, ev[(ev.event_seq == 1) & ev[pid].isin(off.index)].event_id.values,
                              "first event date differs from invoice_date"))
    led = _by(t["ledger_entry"], "ledger_entry_id").entry_date
    for table in ("receivable_status_event", "payable_status_event"):
        ev = t[table]
        linked = ev[ev.ledger_entry_id.notna()]
        n += len(linked)
        bad = linked[linked.ledger_entry_id.map(led) != linked.event_date]
        parts.append(_bad(table, bad.event_id.values, "event_date differs from the date of its ledger entry"))
    return n, _cat(parts)


@check("DATE_SETTLEMENT", "DATE_ORDERING", "A ledger entry that settles a document is not dated before the document, and a reversal is not dated before its original")
def date_settlement(t):
    sl, led = t["settlement_link"], _by(t["ledger_entry"], "ledger_entry_id").entry_date
    parts, n = [], 0
    for table, col, key in (("receivable", "receivable_id", "settlement_link_id"), ("payable", "payable_id", "settlement_link_id")):
        j = sl[sl[col].notna()]
        inv = _by(t[table], col).invoice_date
        n += len(j)
        bad = j[j.ledger_entry_id.map(led) < j[col].map(inv)]
        parts.append(_bad("settlement_link", bad[key].values, f"ledger entry dated before the {table} invoice"))
    rev = t["ledger_entry"][t["ledger_entry"].reverses_ledger_entry_id.notna()]
    n += len(rev)
    bad = rev[rev.entry_date < rev.reverses_ledger_entry_id.map(led)]
    parts.append(_bad("ledger_entry", bad.ledger_entry_id.values, "reversal dated before the entry it reverses"))
    return n, _cat(parts)


@check("DATE_ACTUALS_AFTER_CUTOFF", "DATE_ORDERING", "A forecast-vs-actual row compares against an actual dated after the run cutoff and not after the compared-through date")
def date_actuals(t):
    v = t["forecast_vs_actual"].merge(t["forecast_run"][["forecast_run_id", "cutoff_date"]], on="forecast_run_id", how="left")
    bad = v[(v.actual_date.notna() & (v.actual_date <= v.cutoff_date)) | (v.actual_date > v.compared_through_date)]
    return len(v), _bad("forecast_vs_actual", bad.comparison_id.values, "actual date outside (cutoff, compared_through_date]")


# ---- 6. arithmetic reconciliation -----------------------------------------------------------------------------
@check("POSITION_RECONCILES", "ARITHMETIC_RECONCILIATION",
       "Daily position: closing = opening + credits - debits; opening = previous closing; closing and totals equal the ledger")
def position_reconciles(t):
    breaks = reconcile(t["ledger_entry"], t["daily_cash_position"], t["bank_account"])
    ids = breaks.bank_account_id.astype(str) + "|" + breaks.position_date.astype(str)
    return len(t["daily_cash_position"]), _bad("daily_cash_position", ids.values,
                                                (breaks.check + ": expected " + breaks.expected.astype(str)
                                                 + ", found " + breaks.actual.astype(str)).values)


def _rate_lookup(t):
    r = t["currency_rate"]
    return dict(zip(r.rate_id, r.rate)), dict(zip(r.rate_id, r.quote_currency)), dict(zip(r.rate_id, r.rate_date))


def _base_value(amount, currency, rate, exp):
    return np.rint(amount * rate * 10.0 ** (exp["USD"] - currency.map(exp)))


@check("FX_COMPLETE", "ARITHMETIC_RECONCILIATION", "A non-base-currency amount carries an FX rate and a base amount; a base-currency amount carries neither a rate nor a conversion")
def fx_complete(t):
    cur = t["currency"]
    base = cur.currency_code[cur.is_base.astype(int) == 1].iloc[0]
    n, parts = 0, []
    for table, idc, cc, amt in (("ledger_entry", "ledger_entry_id", "base_amount_minor", None),
                                ("cash_flow_forecast", "cash_flow_forecast_id", "forecast_base_amount_minor", None),
                                ("daily_cash_position", None, "closing_base_minor", None)):
        df = t[table]
        n += len(df)
        foreign = df.currency != base
        ident = _key(df, ["bank_account_id", "position_date"]) if idc is None else df[idc]
        no_rate = foreign & df.fx_rate_id.isna()
        no_base = foreign & df[cc].isna()
        parts.append(_bad(table, ident[no_rate | no_base].values,
                          pd.Series(np.where(no_rate[no_rate | no_base], "no fx_rate_id on a non-base row",
                                             "no base amount on a non-base row"))))
        odd = ~foreign & df.fx_rate_id.notna()
        parts.append(_bad(table, ident[odd].values, "base-currency row carries an fx_rate_id"))
    return n, _cat(parts)


@check("FX_RECOMPUTE", "ARITHMETIC_RECONCILIATION", "Base amount = round(amount x rate x 10^(base exp - local exp)) with the rate the row cites; the rate is for the row's currency and, for actuals, its date")
def fx_recompute(t):
    rate, rcur, rdate = _rate_lookup(t)
    cur = _by(t["currency"], "currency_code").minor_unit_exponent.astype(int)
    n, parts = 0, []
    for table, idc, amt, basec, datec in (("ledger_entry", "ledger_entry_id", "amount_minor", "base_amount_minor", "entry_date"),
                                          ("daily_cash_position", None, "closing_minor", "closing_base_minor", "position_date")):
        df = t[table]
        df = df[df.fx_rate_id.notna() & df[basec].notna()]
        n += len(df)
        ident = _key(df, ["bank_account_id", "position_date"]) if idc is None else df[idc]
        r = df.fx_rate_id.map(rate)
        expected = _base_value(df[amt], df.currency, r, cur)
        wrong = expected != df[basec]
        parts.append(_bad(table, ident[wrong].values,
                          pd.Series([f"base {b} != expected {int(e)}" for b, e in zip(df[basec][wrong], expected[wrong])])))
        parts.append(_bad(table, ident[df.fx_rate_id.map(rcur) != df.currency].values, "rate is for a different currency"))
        parts.append(_bad(table, ident[df.fx_rate_id.map(rdate) != df[datec]].values, f"rate date differs from {datec}"))
    return n, _cat(parts)


@check("SETTLEMENT_ALLOCATION", "ARITHMETIC_RECONCILIATION", "Allocations of a settlement entry sum to its amount; a document is never allocated more than its amount")
def settlement_allocation(t):
    sl, le = t["settlement_link"], t["ledger_entry"]
    alloc = sl.groupby("ledger_entry_id").allocated_amount_minor.sum()
    settle = le[le.entry_type.isin(["RECEIPT_SETTLEMENT", "PAYMENT_SETTLEMENT"])]
    bad_entry = settle[settle.ledger_entry_id.map(alloc).fillna(0) != settle.amount_minor]
    parts = [_bad("ledger_entry", bad_entry.ledger_entry_id.values, "allocations differ from the entry amount")]
    non = le[~le.entry_type.isin(["RECEIPT_SETTLEMENT", "PAYMENT_SETTLEMENT"]) & le.ledger_entry_id.isin(set(sl.ledger_entry_id))]
    parts.append(_bad("ledger_entry", non.ledger_entry_id.values, "non-settlement entry is allocated to a document"))
    n = len(settle)
    for table, col in (("receivable", "receivable_id"), ("payable", "payable_id")):
        total = sl[sl[col].notna()].groupby(col).allocated_amount_minor.sum()
        d = t[table]
        n += len(d)
        over = d[d[col].map(total).fillna(0) > d.amount_minor]
        parts.append(_bad(table, over[col].values, "allocated more than the document amount"))
    return n, _cat(parts)


@check("PROJECTION_ARITHMETIC", "ARITHMETIC_RECONCILIATION", "Projected closing = opening + inflow - outflow; shortfall flag = (closing < buffer); each run-day follows the previous one")
def projection_arithmetic(t):
    p = t["forecast_daily_projection"].sort_values(["forecast_run_id", "entity_id", "projection_date"], kind="stable").reset_index(drop=True)
    ident = _key(p, ["forecast_run_id", "entity_id", "projection_date"])
    arith = p.projected_closing_base_minor != p.opening_base_minor + p.projected_inflow_base_minor - p.projected_outflow_base_minor
    flag = p.shortfall_flag.astype(int) != (p.projected_closing_base_minor < p.liquidity_buffer_base_minor).astype(int)
    prev = p.groupby(["forecast_run_id", "entity_id"]).projected_closing_base_minor.shift(1)
    chain = prev.notna() & (p.opening_base_minor != prev)
    return len(p), _cat([_bad("forecast_daily_projection", ident[arith].values, "closing != opening + inflow - outflow"),
                         _bad("forecast_daily_projection", ident[flag].values, "shortfall_flag disagrees with closing vs buffer"),
                         _bad("forecast_daily_projection", ident[chain].values, "opening != previous day's closing")])


@check("VARIANCE_ARITHMETIC", "ARITHMETIC_RECONCILIATION", "Variance = actual - forecast; amount and FX parts add up to it; date variance = actual date - forecast date")
def variance_arithmetic(t):
    v = t["forecast_vs_actual"]
    a = v.variance_base_minor != v.actual_net_base_minor - v.forecast_net_base_minor
    b = v.amount_variance_base_minor + v.fx_variance_base_minor != v.variance_base_minor
    both = v.actual_date.notna() & v.forecast_date.notna()
    days = (pd.to_datetime(v.actual_date) - pd.to_datetime(v.forecast_date)).dt.days
    c = both & (days != v.date_variance_days)
    return len(v), _cat([_bad("forecast_vs_actual", v.comparison_id[a].values, "variance != actual - forecast"),
                         _bad("forecast_vs_actual", v.comparison_id[b].values, "amount + fx variance != total variance"),
                         _bad("forecast_vs_actual", v.comparison_id[c].values, "date_variance_days wrong")])


@check("REVERSAL_MIRRORS", "ARITHMETIC_RECONCILIATION", "A reversal entry has the original's account and amount and the opposite direction")
def reversal_mirrors(t):
    le = t["ledger_entry"]
    rev = le[le.reverses_ledger_entry_id.notna()]
    rev = rev[rev.reverses_ledger_entry_id.isin(le.ledger_entry_id)]
    o = _by(le, "ledger_entry_id").loc[rev.reverses_ledger_entry_id]
    bad = (o.amount_minor.values != rev.amount_minor.values) | (o.bank_account_id.values != rev.bank_account_id.values) \
        | (o.direction.values == rev.direction.values)
    return len(rev), _bad("ledger_entry", rev.ledger_entry_id[bad].values, "does not mirror the entry it reverses")


# ---- 7. lifecycle ---------------------------------------------------------------------------------------------
def _lifecycle(ev: pd.DataFrame, pid: str, transitions: dict, cause_map: dict, table: str, parent: pd.DataFrame, parent_id: str):
    ev = ev.sort_values([pid, "event_seq"], kind="stable").reset_index(drop=True)
    parts = []
    # contiguous sequence starting at 1
    expected_seq = ev.groupby(pid).cumcount() + 1
    parts.append(_bad(table, ev.event_id[ev.event_seq != expected_seq].values, "event_seq is not 1, 2, 3, ... in order"))
    prev_to = ev.groupby(pid).to_status.shift(1)
    frm = [None if pd.isna(x) else x for x in ev.from_status]
    prv = [None if pd.isna(x) else x for x in prev_to]
    first = (ev.event_seq == 1).to_numpy()
    chain = np.array([(not f) and a != b for f, a, b in zip(first, frm, prv)], dtype=bool)
    parts.append(_bad(table, ev.event_id[chain].values, "from_status differs from the previous event's to_status"))
    bad_tr = [i for i, (f, to) in enumerate(zip(frm, ev.to_status)) if to not in transitions.get(f, set())]
    parts.append(_bad(table, ev.event_id.iloc[bad_tr].values,
                      pd.Series([f"transition {frm[i]} -> {ev.to_status.iloc[i]} not allowed" for i in bad_tr])))
    bad_cause = [i for i, (to, c) in enumerate(zip(ev.to_status, ev.cause)) if c not in cause_map.get(to, set())]
    parts.append(_bad(table, ev.event_id.iloc[bad_cause].values,
                      pd.Series([f"cause {ev.cause.iloc[i]} does not produce {ev.to_status.iloc[i]}" for i in bad_cause])))
    last = ev.groupby(pid).to_status.last()
    cur = _by(parent, parent_id).status
    mism = cur[cur != last.reindex(cur.index)]
    parts.append(_bad(parent_id.replace("_id", ""), mism.index.values, "stored status differs from the last event's to_status"))
    return len(ev), _cat(parts)


@check("LIFECYCLE_RECEIVABLE", "LIFECYCLE_TRANSITIONS", "Receivable events follow the allowed transitions; the stored status is the last event's status")
def lifecycle_receivable(t):
    return _lifecycle(t["receivable_status_event"], "receivable_id", RECEIVABLE_TRANSITIONS, CAUSE_FOR_STATUS,
                      "receivable_status_event", t["receivable"], "receivable_id")


@check("LIFECYCLE_PAYABLE", "LIFECYCLE_TRANSITIONS", "Payable events follow the allowed transitions; the stored status is the last event's status")
def lifecycle_payable(t):
    return _lifecycle(t["payable_status_event"], "payable_id", PAYABLE_TRANSITIONS, CAUSE_FOR_STATUS,
                      "payable_status_event", t["payable"], "payable_id")


@check("LIFECYCLE_RUN", "LIFECYCLE_TRANSITIONS", "Forecast-run events follow the allowed transitions; stored status is the last event's; SUPERSEDED means a later version exists")
def lifecycle_run(t):
    n, bad = _lifecycle(t["forecast_run_status_event"], "forecast_run_id", RUN_TRANSITIONS, RUN_CAUSE_FOR_STATUS,
                        "forecast_run_status_event", t["forecast_run"], "forecast_run_id")
    fr = t["forecast_run"]
    replaced = set(fr.supersedes_run_id.dropna())
    wrong = fr[(fr.status == "SUPERSEDED") != fr.forecast_run_id.isin(replaced)]
    return n + len(fr), _cat([bad, _bad("forecast_run", wrong.forecast_run_id.values, "SUPERSEDED status and supersedes_run_id disagree")])


@check("LIFECYCLE_ACTIVITY_WINDOW", "LIFECYCLE_TRANSITIONS",
       "No ledger entry is dated on or after its entity's suspension, or after the closure of its account or entity")
def lifecycle_activity_window(t):
    le = t["ledger_entry"]
    acct = _by(t["bank_account"], "bank_account_id")
    ent = _by(t["legal_entity"], "entity_id")
    ent_id = le.bank_account_id.map(acct.entity_id)
    susp = ent_id.map(ent.suspended_from)
    ent_end = ent_id.map(ent.effective_to)
    acct_end = le.bank_account_id.map(acct.effective_to)
    after_susp = susp.notna() & (le.entry_date >= susp)
    after_close = (ent_end.notna() & (le.entry_date > ent_end)) | (acct_end.notna() & (le.entry_date > acct_end))
    parts = [_bad("ledger_entry", le.ledger_entry_id[after_susp].values, "posted on or after the entity's suspension"),
             _bad("ledger_entry", le.ledger_entry_id[after_close].values, "posted after the account or entity closed")]
    return len(le), _cat(parts)


@check("LIFECYCLE_SETTLEMENT_STATE", "LIFECYCLE_TRANSITIONS", "A SETTLED document is fully allocated, an OPEN one has no allocation, a PARTIALLY_SETTLED one has some")
def lifecycle_settlement_state(t):
    sl = t["settlement_link"]
    n, parts = 0, []
    for table, col in (("receivable", "receivable_id"), ("payable", "payable_id")):
        d = t[table]
        n += len(d)
        total = d[col].map(sl[sl[col].notna()].groupby(col).allocated_amount_minor.sum()).fillna(0)
        s = d.status
        bad = ((s == "SETTLED") & (total != d.amount_minor)) | ((s == "OPEN") & (total != 0)) \
            | ((s == "PARTIALLY_SETTLED") & ((total <= 0) | (total >= d.amount_minor)))
        parts.append(_bad(table, d[col][bad].values, pd.Series([f"status {a} but allocated {b} of {c}" for a, b, c in
                                                               zip(s[bad], total[bad], d.amount_minor[bad])])))
    return n, _cat(parts)


# ---- 8. duplicates ---------------------------------------------------------------------------------------------
@check("DUP_FORECAST_ITEM", "DUPLICATES", "No forecast item repeats another in the same run: all fields equal except the item id")
def dup_forecast_item(t):
    cf = t["cash_flow_forecast"].sort_values("cash_flow_forecast_id", kind="stable")
    key = _key(cf, [c for c in cf.columns if c != "cash_flow_forecast_id"])
    first = cf.cash_flow_forecast_id.groupby(key.values).transform("first")
    rep = cf[cf.cash_flow_forecast_id != first.values]
    return len(cf), _bad("cash_flow_forecast", rep.cash_flow_forecast_id.values,
                         pd.Series([f"same content as {o}" for o in first[rep.index]]))


@check("DUP_LEDGER_REF", "DUPLICATES", "Bank reference numbers on ledger entries are unique")
def dup_ledger_ref(t):
    le = t["ledger_entry"]
    d = le[le.external_ref.duplicated(keep="first") & le.external_ref.notna()]
    return len(le), _bad("ledger_entry", d.ledger_entry_id.values, "external_ref already used")


@check("DUP_SETTLEMENT_LINK", "DUPLICATES", "The same document is not linked to the same ledger entry twice")
def dup_settlement(t):
    sl = t["settlement_link"]
    key = _key(sl, ["receivable_id", "payable_id", "ledger_entry_id"])
    d = sl[key.duplicated(keep="first")]
    return len(sl), _bad("settlement_link", d.settlement_link_id.values, "document and entry already linked")


@check("DUP_DOCUMENT", "DUPLICATES", "A counterparty does not have two receivables (payables) with the same document reference and amount")
def dup_document(t):
    n, parts = 0, []
    for table, idc, name in (("receivable", "receivable_id", "counterparty_name"), ("payable", "payable_id", "payee_name")):
        d = t[table]
        n += len(d)
        key = _key(d, ["entity_id", name, "document_ref", "amount_minor", "invoice_date"])
        parts.append(_bad(table, d[idc][key.duplicated(keep="first")].values, "same entity, counterparty, document, amount and date"))
    return n, _cat(parts)


# ---- 9. currency ------------------------------------------------------------------------------------------------
@check("CURRENCY_ACCOUNT", "CURRENCY_CONSISTENCY", "A row's currency equals the currency of its bank account")
def currency_account(t):
    acct = _by(t["bank_account"], "bank_account_id").currency
    n, parts = 0, []
    for table, idc in (("ledger_entry", "ledger_entry_id"), ("cash_flow_forecast", "cash_flow_forecast_id"),
                       ("receivable", "receivable_id"), ("payable", "payable_id")):
        d = t[table]
        n += len(d)
        want = d.bank_account_id.map(acct)
        bad = d[(d.currency != want) & want.notna()]
        parts.append(_bad(table, bad[idc].values,
                          pd.Series([f"currency {a}, account currency {b}" for a, b in zip(bad.currency, want[bad.index])])))
    p = t["daily_cash_position"]
    n += len(p)
    want = p.bank_account_id.map(acct)
    bad = p[(p.currency != want) & want.notna()]
    parts.append(_bad("daily_cash_position", _key(bad, ["bank_account_id", "position_date"]).values, "currency differs from the account's"))
    return n, _cat(parts)


@check("CURRENCY_CODES", "CURRENCY_CONSISTENCY", "Currency codes used anywhere exist in the currency table and rates quote the base currency")
def currency_codes(t):
    known = set(t["currency"].currency_code)
    n, parts = 0, []
    for table, col, idc in (("ledger_entry", "currency", "ledger_entry_id"), ("receivable", "currency", "receivable_id"),
                            ("payable", "currency", "payable_id"), ("bank_account", "currency", "bank_account_id"),
                            ("currency_rate", "quote_currency", "rate_id")):
        d = t[table]
        n += len(d)
        parts.append(_bad(table, d[idc][~d[col].isin(known)].values, f"{col} not in the currency table"))
    r = t["currency_rate"]
    base = t["currency"].currency_code[t["currency"].is_base.astype(int) == 1].iloc[0]
    parts.append(_bad("currency_rate", r.rate_id[r.base_currency != base].values, "rate base is not the base currency"))
    parts.append(_bad("currency_rate", r.rate_id[~(r.rate > 0)].values, "rate is not positive"))
    return n, _cat(parts)


# ---------------------------------------------------------------------------------------------
@dataclass
class CheckOutcome:
    check_id: str
    category: str
    description: str
    n_checked: int
    violations: pd.DataFrame

    @property
    def n_violations(self) -> int:
        return len(self.violations)


def run_checks(tables: dict[str, pd.DataFrame]) -> list[CheckOutcome]:
    out = []
    for c in REGISTRY:
        try:
            n, v = c.fn(tables)
        except Exception as exc:        # a check that cannot run is a failure, never a silent pass
            n, v = 0, _bad("(check)", [c.check_id], f"check could not run: {type(exc).__name__}: {exc}")
        v = v.copy()
        v.insert(0, "category", c.category)
        v.insert(0, "check_id", c.check_id)
        out.append(CheckOutcome(c.check_id, c.category, c.description, int(n), v[VIOLATION_COLUMNS]))
    return out
