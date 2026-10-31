"""Command line entry point.

    python -m src.generate --scale full|starter [--seed N] [--out data/]

The CLI resolves and validates the configuration, records it (``run_config.json``), generates every table
(``<out>/input/`` as CSV, plus JSON, SQL seed and SQLite), the derived reports (``<out>/derived/``), the
ground truth (``<out>/ground_truth/``) and the source manifest. The output is a pure
function of the arguments: no timestamps, hostnames or absolute paths are
written, so two runs with the same arguments produce byte-identical files.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import DEFAULT_SEED, SCALES, Config, build_config
from .compare.metrics import accuracy_reports
from .export import copy_schema, write_csv_tables, write_json_tables, write_sql_seed, write_sqlite
from .manifest import write_manifest
from .pipeline import generate_dataset
from .txn.derive import consolidate


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.generate",
        description="Generate the synthetic liquidity-forecasting dataset.",
    )
    p.add_argument("--scale", choices=sorted(SCALES), default="full",
                   help="named size preset (default: full)")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"root random seed (default: {DEFAULT_SEED})")
    p.add_argument("--out", type=Path, default=Path("data"),
                   help="output directory (default: data/)")
    p.add_argument("--no-anomalies", action="store_true",
                   help="write the valid baseline only (ground truth is then empty)")
    group = p.add_argument_group("optional size overrides (replace the preset value)")
    group.add_argument("--n-entities", type=int)
    group.add_argument("--n-accounts", type=int)
    group.add_argument("--n-ledger", type=int)
    group.add_argument("--n-forecast-items", type=int)
    return p


def canonical_command(args: argparse.Namespace) -> str:
    """The reproducible command line. ``--out`` is left out on purpose: where the
    files land does not change what they contain."""
    parts = ["python", "-m", "src.generate", "--scale", args.scale, "--seed", str(args.seed)]
    if args.no_anomalies:
        parts.append("--no-anomalies")
    for flag, value in (("--n-entities", args.n_entities), ("--n-accounts", args.n_accounts),
                        ("--n-ledger", args.n_ledger),
                        ("--n-forecast-items", args.n_forecast_items)):
        if value is not None:
            parts += [flag, str(value)]
    return " ".join(parts)


def write_ground_truth(truth, directory: Path) -> None:
    """ground_truth.csv / .json live in their own directory, never under input/."""
    directory.mkdir(parents=True, exist_ok=True)
    write_csv_tables({"ground_truth": truth}, directory)
    records = json.loads(truth.to_json(orient="records"))
    for r in records:
        r["injected_changes"] = json.loads(r["injected_changes"])
    (directory / "ground_truth.json").write_text(json.dumps(records, indent=2, sort_keys=True) + "\n",
                                                 encoding="utf-8")


def run(config: Config, out_dir: Path, command: str, inject: bool = True) -> Path:
    """Generate and write everything built so far. Returns the run-record path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = generate_dataset(config, inject)
    write_csv_tables(data.tables, out_dir / "input")
    write_json_tables(data.tables, out_dir / "json")
    copy_schema(out_dir / "sql")
    write_sql_seed(data.tables, out_dir / "sql" / "seed_data.sql")
    write_sqlite(data.tables, out_dir / "db" / "liquidity.sqlite")
    # derived roll-ups and accuracy reports are computed from the VALID baseline
    write_csv_tables({"consolidated_daily_position": consolidate(
        data.baseline["daily_cash_position"], data.baseline["bank_account"])}, out_dir / "derived")
    write_ground_truth(data.ground_truth, out_dir / "ground_truth")
    record = {
        "generator_command": command,
        "config_fingerprint": config.fingerprint(),
        "config": config.to_dict(),
    }
    reports = accuracy_reports(config, data.baseline)
    write_csv_tables(reports, out_dir / "derived")
    by_scenario = reports["forecast_accuracy_by_scenario"].astype(object).where(
        reports["forecast_accuracy_by_scenario"].notna(), None)
    (out_dir / "derived" / "forecast_accuracy_by_scenario.json").write_text(
        json.dumps(by_scenario.to_dict(orient="records"), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path = out_dir / "run_config.json"
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # the manifest goes last: it lists (and hashes) every other file
    write_manifest(out_dir, command, config, {k: len(v) for k, v in data.tables.items()})
    return path


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = build_config(
            args.scale,
            seed=args.seed,
            n_entities=args.n_entities,
            n_accounts=args.n_accounts,
            n_ledger=args.n_ledger,
            n_forecast_items=args.n_forecast_items,
        )
    except ValueError as exc:
        parser.error(str(exc))  # prints usage + message, exits with status 2

    path = run(config, args.out, canonical_command(args), inject=not args.no_anomalies)
    print(f"scale={config.scale_name} seed={config.seed} "
          f"entities={config.n_entities} accounts={config.n_accounts} "
          f"ledger={config.n_ledger} forecast_items={config.n_forecast_items} "
          f"currencies={','.join(config.currency_codes)}")
    print(f"config fingerprint {config.fingerprint()[:16]}")
    print(f"wrote {path}")
    print("note: tables written and anomalies injected; run `python -m src.detect --data DIR` "
          "then `python -m src.detect.evaluate --data DIR` for the exception queue and its evaluation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
