"""SQLite helpers: build a database from sql/schema.sql and load DataFrames into it."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "sql" / "schema.sql"

# Parents before children. Tables absent from a given run are simply skipped.
TABLE_ORDER = (
    "currency", "legal_entity", "bank_account", "currency_rate",
    "receivable", "payable", "ledger_entry", "settlement_link",
    "receivable_status_event", "payable_status_event",
    "liquidity_scenario", "forecast_run", "forecast_run_status_event",
    "cash_flow_forecast", "daily_cash_position", "forecast_daily_projection",
    "forecast_vs_actual",
)


def create_database(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _python_rows(df: pd.DataFrame):
    """Rows as plain Python values: numpy scalars would not bind, and NaN must be NULL."""
    clean = df.astype(object).where(df.notna(), None)
    return list(clean.itertuples(index=False, name=None))


def load_tables(conn: sqlite3.Connection, tables: dict[str, pd.DataFrame]) -> None:
    """Insert tables in dependency order inside one transaction.

    Foreign keys are checked at commit (deferred) so that rows may reference
    parents that appear later in the same table, e.g. legal_entity.parent_entity_id.
    A violation raises sqlite3.IntegrityError at commit and nothing is kept.
    """
    unknown = set(tables) - set(TABLE_ORDER)
    if unknown:
        raise ValueError(f"unknown tables: {sorted(unknown)}")
    try:
        conn.execute("BEGIN")
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for name in TABLE_ORDER:
            if name not in tables:
                continue
            df = tables[name]
            cols = ", ".join(df.columns)
            marks = ", ".join("?" for _ in df.columns)
            conn.executemany(f"INSERT INTO {name} ({cols}) VALUES ({marks})", _python_rows(df))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
