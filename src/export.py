"""File exports: CSV, JSON, SQL seed and SQLite, all written from the same DataFrames."""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def write_csv_tables(tables: dict[str, pd.DataFrame], directory: Path) -> list[Path]:
    """One CSV per table. Formatting is fixed (LF line ends, rates at 8 decimals,
    NULL as empty) so identical data always gives byte-identical files."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, df in tables.items():
        path = directory / f"{name}.csv"
        df.to_csv(path, index=False, lineterminator="\n", float_format="%.8f", encoding="utf-8")
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------------------------
# JSON, SQL seed and SQLite. All three are pure functions of the tables: no clock, no host name.
import json
import math
import sqlite3

import numpy as np

from .db import SCHEMA_PATH, TABLE_ORDER, create_database, load_tables


def _py(v):
    """A plain Python value (or None) for a cell."""
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, float) and v.is_integer() and abs(v) < 2**53:
        return v          # keep as float: the column is a float column
    return v


def _records(df: pd.DataFrame):
    clean = df.astype(object).where(df.notna(), None)
    for row in clean.itertuples(index=False, name=None):
        yield [_py(v) for v in row]


def ordered(tables: dict[str, pd.DataFrame]) -> list[str]:
    """Tables in dependency order (parents first), then any extras by name."""
    return [n for n in TABLE_ORDER if n in tables] + sorted(n for n in tables if n not in TABLE_ORDER)


def write_json_tables(tables: dict[str, pd.DataFrame], directory: Path) -> list[Path]:
    """One JSON array of objects per table, one object per line (valid JSON, diff friendly)."""
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ordered(tables):
        df = tables[name]
        cols = list(df.columns)
        lines = [json.dumps(dict(zip(cols, r)), allow_nan=False, ensure_ascii=False, separators=(",", ":"))
                 for r in _records(df)]
        path = directory / f"{name}.json"
        path.write_text("[\n" + ",\n".join(lines) + "\n]\n" if lines else "[]\n", encoding="utf-8", newline="\n")
        paths.append(path)
    return paths


def sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


SEED_HEADER = """-- Seed data for the liquidity-forecasting dataset (synthetic; no real data).
-- Load AFTER sql/schema.sql:   sqlite3 liquidity.db < sql/schema.sql && sqlite3 liquidity.db < seed_data.sql
-- Rows are inserted parents-first inside one transaction; foreign keys are checked at COMMIT.
"""


def write_sql_seed(tables: dict[str, pd.DataFrame], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = [SEED_HEADER, "PRAGMA foreign_keys = ON;", "BEGIN;", "PRAGMA defer_foreign_keys = ON;"]
    for name in ordered(tables):
        df = tables[name]
        cols = ", ".join(df.columns)
        out.append(f"-- {name}: {len(df)} rows")
        for r in _records(df):
            out.append(f"INSERT INTO {name} ({cols}) VALUES ({', '.join(sql_literal(v) for v in r)});")
    out.append("COMMIT;")
    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    return path


def write_sqlite(tables: dict[str, pd.DataFrame], path: Path) -> Path:
    """Fresh database file: schema.sql then the tables. Replaces any existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = create_database(path)
    try:
        load_tables(conn, tables)
        conn.execute("VACUUM")
    finally:
        conn.close()
    return path


def copy_schema(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / "schema.sql"
    dest.write_text(SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return dest
