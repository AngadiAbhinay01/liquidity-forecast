import json

import pytest

from src.config import DEFAULT_SEED, build_config
from src.generate import main


def run_cli(out, *args):
    assert main(["--out", str(out), *args]) == 0
    return json.loads((out / "run_config.json").read_text())


def test_defaults_are_full_scale_and_the_brief_seed(tmp_path):
    rec = run_cli(tmp_path)
    assert rec["config"]["scale_name"] == "full"
    assert rec["config"]["seed"] == DEFAULT_SEED
    assert rec["generator_command"] == "python -m src.generate --scale full --seed 20261022"
    assert rec["config_fingerprint"] == build_config("full").fingerprint()


def test_scale_and_seed_flags(tmp_path):
    rec = run_cli(tmp_path, "--scale", "starter", "--seed", "99")
    assert rec["config"]["n_ledger"] == 3000
    assert rec["config"]["seed"] == 99
    assert rec["generator_command"] == "python -m src.generate --scale starter --seed 99"


def test_size_overrides_are_applied_and_recorded_in_the_command(tmp_path):
    rec = run_cli(tmp_path, "--scale", "starter", "--n-ledger", "500", "--n-forecast-items", "50")
    assert (rec["config"]["n_ledger"], rec["config"]["n_forecast_items"]) == (500, 50)
    assert rec["generator_command"].endswith("--n-ledger 500 --n-forecast-items 50")


TABLES = ("currency", "currency_rate", "legal_entity", "bank_account", "receivable", "payable",
          "ledger_entry", "settlement_link", "receivable_status_event", "payable_status_event",
          "daily_cash_position", "liquidity_scenario", "forecast_run", "forecast_run_status_event",
          "cash_flow_forecast", "forecast_daily_projection", "forecast_vs_actual")
EXPECTED_FILES = {"generator_command.txt", "source_manifest.json", "source_manifest.md", "sql/schema.sql",
                  "sql/seed_data.sql", "db/liquidity.sqlite"} | {f"json/{t}.json" for t in TABLES} | {"run_config.json", "ground_truth/ground_truth.csv", "ground_truth/ground_truth.json", "derived/consolidated_daily_position.csv",
                  "derived/forecast_accuracy_by_scenario.json"} | {
    f"derived/{t}.csv" for t in ("forecast_variance_daily", "forecast_accuracy_by_run",
                                 "forecast_accuracy_by_scenario", "variance_driver_summary")} | {
    f"input/{t}.csv" for t in (
        "currency", "currency_rate", "legal_entity", "bank_account", "receivable", "payable",
        "ledger_entry", "settlement_link", "receivable_status_event", "payable_status_event",
        "daily_cash_position", "liquidity_scenario", "forecast_run", "forecast_run_status_event",
        "cash_flow_forecast", "forecast_daily_projection", "forecast_vs_actual")}


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_input_tables_are_written_as_csv(tmp_path):
    run_cli(tmp_path, "--scale", "starter")
    assert set(tree(tmp_path)) == EXPECTED_FILES
    header = (tmp_path / "input" / "bank_account.csv").read_text().splitlines()[0]
    assert header.startswith("bank_account_id,entity_id,account_seq")
    assert len((tmp_path / "input" / "bank_account.csv").read_text().splitlines()) == 1 + 18


def test_same_arguments_give_byte_identical_output_in_different_directories(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    run_cli(a, "--scale", "starter")
    run_cli(b, "--scale", "starter")
    assert tree(a) == tree(b)


def test_different_seeds_give_different_data_files(tmp_path):
    run_cli(tmp_path / "a", "--scale", "starter")
    run_cli(tmp_path / "b", "--scale", "starter", "--seed", "1")
    ta, tb = tree(tmp_path / "a"), tree(tmp_path / "b")
    for name in ("input/legal_entity.csv", "input/bank_account.csv", "input/currency_rate.csv"):
        assert ta[name] != tb[name], name
    assert ta["input/currency.csv"] == tb["input/currency.csv"]


def test_output_leaks_no_paths_or_timestamps(tmp_path):
    run_cli(tmp_path / "somewhere", "--scale", "starter")
    text = (tmp_path / "somewhere" / "run_config.json").read_text()
    assert str(tmp_path) not in text and "somewhere" not in text


def test_a_different_seed_changes_the_fingerprint(tmp_path):
    a = run_cli(tmp_path / "a", "--scale", "starter")
    b = run_cli(tmp_path / "b", "--scale", "starter", "--seed", "1")
    assert a["config_fingerprint"] != b["config_fingerprint"]


def test_output_directory_is_created_including_parents(tmp_path):
    out = tmp_path / "x" / "y" / "z"
    run_cli(out, "--scale", "starter")
    assert out.is_dir()


def test_unknown_scale_exits_with_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--out", str(tmp_path), "--scale", "huge"])
    assert e.value.code == 2


def test_invalid_config_exits_with_a_message_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "never"
    with pytest.raises(SystemExit) as e:
        main(["--out", str(out), "--scale", "starter", "--n-accounts", "2"])
    assert e.value.code == 2
    assert "n_accounts must be >= n_entities" in capsys.readouterr().err
    assert not out.exists()


def test_negative_seed_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as e:
        main(["--out", str(tmp_path), "--seed", "-5"])
    assert e.value.code == 2
