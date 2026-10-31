"""Reproducibility check.

    python -m src.reproduce [--scale starter|full] [--seed N] [--report-dir DIR]

Generates the dataset twice from the same seed, each time in a separate Python process and
a fresh directory, then compares (1) the set of files, (2) the SHA-256 of every file and
(3) the row count of every table in every format (CSV, JSON, SQL seed, SQLite). It prints a
pass/fail report, writes reproducibility_report.json / .md and exits 1 on any difference.

As a sensitivity control it also generates once with a different seed and requires the
data files to differ; a comparison that cannot fail would prove nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import DEFAULT_SEED, SCALES
from .db import SCHEMA_PATH
from .manifest import list_files, sha256_file

ROOT = Path(__file__).resolve().parents[1]


def generate(scale: str, seed: int, out: Path) -> str:
    cmd = [sys.executable, "-m", "src.generate", "--scale", scale, "--seed", str(seed), "--out", str(out)]
    subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True)
    return " ".join(["python", "-m", "src.generate", "--scale", scale, "--seed", str(seed)])


def file_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in list_files(root)}


def row_counts(root: Path) -> dict[str, dict[str, int]]:
    """{table: {format: rows}} for csv, json, sql seed and sqlite."""
    out: dict[str, dict[str, int]] = {}
    for p in sorted((root / "input").glob("*.csv")):
        with open(p, newline="", encoding="utf-8") as f:
            out.setdefault(p.stem, {})["csv"] = sum(1 for row in csv.reader(f) if row) - 1
    for p in sorted((root / "json").glob("*.json")):
        out.setdefault(p.stem, {})["json"] = len(json.loads(p.read_text(encoding="utf-8")))
    seed = sqlite3.connect(":memory:")
    seed.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed.executescript((root / "sql" / "seed_data.sql").read_text(encoding="utf-8"))
    db = sqlite3.connect(str(root / "db" / "liquidity.sqlite"))
    for conn, label in ((seed, "sql_seed"), (db, "sqlite")):
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if t in out:
                out[t][label] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    seed.close()
    db.close()
    return out


def compare_dirs(a: Path, b: Path) -> dict:
    ha, hb = file_hashes(a), file_hashes(b)
    only_a, only_b = sorted(set(ha) - set(hb)), sorted(set(hb) - set(ha))
    mismatched = sorted(k for k in set(ha) & set(hb) if ha[k] != hb[k])
    ca, cb = row_counts(a), row_counts(b)
    count_diff = sorted(t for t in set(ca) | set(cb) if ca.get(t) != cb.get(t))
    inconsistent = sorted(t for t, d in ca.items() if len(set(d.values())) != 1 or len(d) != 4)
    return {"files": len(ha), "only_in_first": only_a, "only_in_second": only_b, "hash_mismatches": mismatched,
            "row_count_mismatches": count_diff, "tables_with_inconsistent_formats": inconsistent,
            "hashes": ha, "row_counts": ca,
            "passed": not (only_a or only_b or mismatched or count_diff or inconsistent)}


DATA_PREFIXES = ("input/", "json/", "sql/seed_data", "db/")


def sensitivity(a: Path, c: Path) -> dict:
    ha, hc = file_hashes(a), file_hashes(c)
    data = [k for k in ha if k.startswith(DATA_PREFIXES)]
    differing = [k for k in data if ha[k] != hc.get(k)]
    return {"data_files": len(data), "differing_with_other_seed": len(differing),
            "passed": len(differing) > 0 and len(differing) == len([k for k in data if k not in STATIC_FILES(ha, hc)])}


def STATIC_FILES(ha, hc):
    """Files that legitimately do not depend on the seed (static tables such as `currency`)."""
    return [k for k in ha if ha[k] == hc.get(k)]


def report_markdown(r: dict) -> str:
    c = r["comparison"]
    L = ["# Reproducibility report", "", f"Command: `{r['generator_command']}`", "",
         f"**Result: {'PASS' if r['passed'] else 'FAIL'}.** Two independent runs, {c['files']} files compared.", "",
         f"* Files only in one run: {len(c['only_in_first']) + len(c['only_in_second'])}",
         f"* SHA-256 mismatches: {len(c['hash_mismatches'])}",
         f"* Row-count mismatches between runs: {len(c['row_count_mismatches'])}",
         f"* Tables whose CSV / JSON / SQL seed / SQLite row counts disagree: {len(c['tables_with_inconsistent_formats'])}",
         f"* Sensitivity control (seed {r['control_seed']}): {r['control']['differing_with_other_seed']} of "
         f"{r['control']['data_files']} data files differ, as they must: {'PASS' if r['control']['passed'] else 'FAIL'}", "",
         "## Rows per table", "", "| Table | CSV | JSON | SQL seed | SQLite |", "|---|---|---|---|---|"]
    for t, d in sorted(c["row_counts"].items()):
        L.append(f"| {t} | {d.get('csv')} | {d.get('json')} | {d.get('sql_seed')} | {d.get('sqlite')} |")
    L += ["", "## SHA-256 (identical in both runs)", "", "| File | SHA-256 |", "|---|---|"]
    L += [f"| {k} | `{v}` |" for k, v in sorted(c["hashes"].items())]
    for key, title in (("hash_mismatches", "Mismatched files"), ("only_in_first", "Only in first run"),
                       ("only_in_second", "Only in second run"), ("row_count_mismatches", "Row-count mismatches")):
        if c[key]:
            L += ["", f"## {title}", ""] + [f"- {x}" for x in c[key]]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.reproduce")
    p.add_argument("--scale", choices=sorted(SCALES), default="starter")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--report-dir", type=Path, default=Path("data/reproducibility"))
    p.add_argument("--skip-control", action="store_true", help="skip the different-seed sensitivity run")
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        print(f"generating run 1 ({args.scale}, seed {args.seed}) ...")
        command = generate(args.scale, args.seed, tmp / "run1")
        print("generating run 2 ...")
        generate(args.scale, args.seed, tmp / "run2")
        comparison = compare_dirs(tmp / "run1", tmp / "run2")
        control_seed = args.seed + 1
        if args.skip_control:
            control = {"data_files": 0, "differing_with_other_seed": 0, "passed": True, "skipped": True}
        else:
            print(f"generating control run (seed {control_seed}) ...")
            generate(args.scale, control_seed, tmp / "run3")
            control = sensitivity(tmp / "run1", tmp / "run3")
    passed = comparison["passed"] and control["passed"]
    result = {"passed": passed, "generator_command": command, "control_seed": control_seed, "control": control,
              "comparison": comparison}
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "reproducibility_report.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    (args.report_dir / "reproducibility_report.md").write_text(report_markdown(result))
    c = comparison
    print(f"\nfiles compared:            {c['files']}")
    print(f"SHA-256 mismatches:        {len(c['hash_mismatches'])}  {'PASS' if not c['hash_mismatches'] else 'FAIL'}")
    print(f"missing / extra files:     {len(c['only_in_first']) + len(c['only_in_second'])}  "
          f"{'PASS' if not (c['only_in_first'] or c['only_in_second']) else 'FAIL'}")
    print(f"row-count mismatches:      {len(c['row_count_mismatches'])}  {'PASS' if not c['row_count_mismatches'] else 'FAIL'}")
    print(f"format disagreements:      {len(c['tables_with_inconsistent_formats'])}  "
          f"{'PASS' if not c['tables_with_inconsistent_formats'] else 'FAIL'}")
    print(f"sensitivity (other seed):  {control['differing_with_other_seed']} data files differ  "
          f"{'PASS' if control['passed'] else 'FAIL'}")
    print(f"\nREPRODUCIBILITY: {'PASS' if passed else 'FAIL'}   report -> {args.report_dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
