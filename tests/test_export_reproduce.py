"""Exports (CSV / JSON / SQL seed / SQLite), manifest and the reproducibility check."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src import reproduce
from src.config import build_config
from src.db import SCHEMA_PATH, create_database, load_tables
from src.export import sql_literal, write_json_tables, write_sql_seed, write_sqlite
from src.generate import main as generate_main
from src.manifest import GENERATED, list_files, sha256_file
from src.pipeline import generate_dataset

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("currency", "currency_rate", "legal_entity", "bank_account", "receivable", "payable", "ledger_entry",
          "settlement_link", "receivable_status_event", "payable_status_event", "daily_cash_position",
          "liquidity_scenario", "forecast_run", "forecast_run_status_event", "cash_flow_forecast",
          "forecast_daily_projection", "forecast_vs_actual")


@pytest.fixture(scope="module")
def out(tmp_path_factory):
    d = tmp_path_factory.mktemp("gen")
    generate_main(["--scale", "starter", "--out", str(d)])
    return d


@pytest.fixture(scope="module")
def dataset():
    return generate_dataset(build_config("starter"))


def all_rows(conn, table):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return cols, sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)


# ---- reproducibility across processes ------------------------------------------------------------------
def test_separate_processes_with_different_hash_seeds_give_identical_files(tmp_path):
    """Regression: set iteration order (per-process string hash seed) once leaked into the data."""
    digests = []
    for i, seed in enumerate(("0", "1", "12345")):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run([sys.executable, "-m", "src.generate", "--scale", "starter", "--out", str(tmp_path / str(i))],
                       cwd=ROOT, env=env, check=True, capture_output=True)
        digests.append({p.relative_to(tmp_path / str(i)).as_posix(): sha256_file(p) for p in list_files(tmp_path / str(i))})
    assert digests[0] == digests[1] == digests[2]


def test_the_comparison_script_passes_on_the_real_generator(tmp_path):
    code = reproduce.main(["--scale", "starter", "--report-dir", str(tmp_path)])
    assert code == 0
    r = json.loads((tmp_path / "reproducibility_report.json").read_text())
    assert r["passed"] and r["comparison"]["hash_mismatches"] == [] and r["control"]["differing_with_other_seed"] > 0
    assert (tmp_path / "reproducibility_report.md").read_text().startswith("# Reproducibility report")


def test_comparison_detects_a_changed_file_a_missing_file_and_a_row_count_change(out, tmp_path):
    import shutil
    other = tmp_path / "other"
    shutil.copytree(out, other)
    assert reproduce.compare_dirs(out, other)["passed"]
    (other / "input" / "currency.csv").write_text((out / "input" / "currency.csv").read_text() + "\n")   # same rows, new bytes
    r = reproduce.compare_dirs(out, other)
    assert r["hash_mismatches"] == ["input/currency.csv"] and not r["passed"] and r["row_count_mismatches"] == []
    shutil.copytree(out, tmp_path / "o2")
    (tmp_path / "o2" / "json" / "payable.json").unlink()
    assert "json/payable.json" in reproduce.compare_dirs(out, tmp_path / "o2")["only_in_first"]
    shutil.copytree(out, tmp_path / "o3")
    lines = (tmp_path / "o3" / "input" / "currency.csv").read_text().splitlines()
    (tmp_path / "o3" / "input" / "currency.csv").write_text("\n".join(lines[:-1]) + "\n")
    r = reproduce.compare_dirs(tmp_path / "o3", out)          # the damaged run first: its CSV now disagrees with its JSON
    assert "currency" in r["row_count_mismatches"] and "currency" in r["tables_with_inconsistent_formats"]


def test_the_sensitivity_control_fails_when_two_runs_are_identical(out):
    assert not reproduce.sensitivity(out, out)["passed"]


# ---- the formats agree with each other ---------------------------------------------------------------------
def test_row_counts_agree_across_the_four_formats_and_with_the_dataset(out, dataset):
    counts = reproduce.row_counts(out)
    assert set(counts) == set(TABLES)
    for t, d in counts.items():
        assert set(d) == {"csv", "json", "sql_seed", "sqlite"}
        assert set(d.values()) == {len(dataset.tables[t])}, (t, d)


def _norm(v):
    if v is None or (isinstance(v, float) and v != v) or v is pd.NA:
        return None
    if hasattr(v, "item"):
        v = v.item()
    return int(v) if isinstance(v, float) and v.is_integer() else v


def test_json_round_trips_to_the_same_values_as_the_dataframes(out, dataset):
    for t in ("bank_account", "ledger_entry", "cash_flow_forecast", "forecast_vs_actual", "receivable"):
        rows = json.loads((out / "json" / f"{t}.json").read_text(encoding="utf-8"))
        df = dataset.tables[t]
        assert len(rows) == len(df) and list(rows[0]) == list(df.columns)
        expected = [{c: _norm(v) for c, v in zip(df.columns, r)} for r in df.itertuples(index=False, name=None)]
        assert [{c: _norm(v) for c, v in r.items()} for r in rows] == expected, t


def test_json_has_real_nulls_not_nan_strings(out):
    text = (out / "json" / "ledger_entry.json").read_text()
    assert "NaN" not in text and "null" in text


def test_sql_seed_loads_into_the_schema_and_equals_the_direct_load(out, dataset):
    a = sqlite3.connect(":memory:")
    a.executescript(SCHEMA_PATH.read_text())
    a.executescript((out / "sql" / "seed_data.sql").read_text())
    b = create_database()
    load_tables(b, dataset.tables)
    c = sqlite3.connect(str(out / "db" / "liquidity.sqlite"))
    for t in TABLES:
        assert all_rows(a, t) == all_rows(b, t) == all_rows(c, t), t


def test_the_shipped_schema_copy_is_the_repo_schema(out):
    assert (out / "sql" / "schema.sql").read_text() == SCHEMA_PATH.read_text()


def test_sqlite_file_holds_the_append_only_triggers_and_no_ground_truth(out):
    c = sqlite3.connect(str(out / "db" / "liquidity.sqlite"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE ledger_entry SET narrative = 'x' WHERE ledger_entry_id = (SELECT MIN(ledger_entry_id) FROM ledger_entry)")
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("truth" in n for n in names) and c.execute("PRAGMA foreign_key_check").fetchall() == []


def test_database_views_flag_exactly_the_labelled_rows_in_the_shipped_db(out, dataset):
    c = sqlite3.connect(str(out / "db" / "liquidity.sqlite"))
    fx = {r[0] for r in c.execute("SELECT record_id FROM v_currency_integrity_issues WHERE issue='FX_RATE_MISSING'")}
    gt = dataset.ground_truth
    assert fx == set(gt[gt.issue_type == "FX_RATE_MISSING"].record_id)


def test_sql_literals_escape_and_null():
    assert sql_literal(None) == "NULL" and sql_literal("O'Brien") == "'O''Brien'" and sql_literal(5) == "5"
    assert sql_literal(0.1) == "0.1" and sql_literal(True) == "1"


def test_text_with_quotes_and_newlines_survives_the_seed_and_json(tmp_path):
    df = pd.DataFrame({"currency_code": ["XQ'T"], "currency_name": ["multi\nline \"x\""], "minor_unit_exponent": [2], "is_base": [0]})
    write_sql_seed({"currency": df}, tmp_path / "s.sql")
    write_json_tables({"currency": df}, tmp_path / "j")
    con = sqlite3.connect(":memory:")          # plain table: this test is about quoting, not the schema's checks
    con.execute("CREATE TABLE currency (currency_code, currency_name, minor_unit_exponent, is_base)")
    con.executescript((tmp_path / "s.sql").read_text())
    assert con.execute("SELECT currency_code, currency_name FROM currency").fetchall() == [("XQ'T", 'multi\nline "x"')]
    assert json.loads((tmp_path / "j" / "currency.json").read_text())[0]["currency_name"] == 'multi\nline "x"'


def test_sqlite_file_is_replaced_not_appended(dataset, tmp_path):
    p = tmp_path / "x.sqlite"
    write_sqlite(dataset.tables, p)
    write_sqlite(dataset.tables, p)
    c = sqlite3.connect(str(p))
    assert c.execute("SELECT COUNT(*) FROM legal_entity").fetchone()[0] == len(dataset.tables["legal_entity"])


# ---- manifest ---------------------------------------------------------------------------------------------
def test_manifest_lists_every_generated_file_with_correct_hashes(out):
    m = json.loads((out / "source_manifest.json").read_text())
    listed = {f["path"]: f for f in m["files"]}
    on_disk = {p.relative_to(out).as_posix() for p in list_files(out)}
    assert set(listed) == on_disk
    for path, f in listed.items():
        assert f["sha256"] == hashlib.sha256((out / path).read_bytes()).hexdigest() and f["bytes"] == (out / path).stat().st_size
    assert "source_manifest.json" not in listed


def test_manifest_states_synthetic_origin_and_the_exact_command(out):
    m = json.loads((out / "source_manifest.json").read_text())
    assert m["synthetic"] is True and m["external_sources"] == []
    assert m["generator_command"] == "python -m src.generate --scale starter --seed 20261022"
    assert (out / "generator_command.txt").read_text().strip() == m["generator_command"]
    assert json.loads((out / "run_config.json").read_text())["generator_command"] == m["generator_command"]
    assert {t["name"]: t["rows"] for t in m["tables"]}["ledger_entry"] == 3000
    assert "synthetic" in (out / "source_manifest.md").read_text().lower()


def test_manifest_ignores_other_steps_output_in_the_same_directory(tmp_path):
    d = tmp_path / "d"
    generate_main(["--scale", "starter", "--out", str(d)])
    first = (d / "source_manifest.json").read_bytes()
    (d / "validation").mkdir()
    (d / "validation" / "validation_report.md").write_text("stale")
    (d / "exceptions").mkdir()
    (d / "exceptions" / "q.csv").write_text("stale")
    generate_main(["--scale", "starter", "--out", str(d)])
    assert (d / "source_manifest.json").read_bytes() == first
    assert {Path(p).parts[0] for p in (f["path"] for f in json.loads(first)["files"])} <= set(GENERATED)


def test_manifest_has_no_paths_or_timestamps(out):
    text = (out / "source_manifest.json").read_text()
    assert str(out) not in text and "/tmp" not in text
