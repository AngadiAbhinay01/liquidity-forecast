# Exports and reproducibility

`python -m src.generate --scale full|starter --out DIR` writes:

| Path | Contents |
|---|---|
| `input/*.csv` | One CSV per table (17 tables). NULL is empty, LF line ends, rates at 8 decimals |
| `json/*.json` | The same tables as JSON arrays, one object per line, real `null`s |
| `sql/schema.sql` | Copy of the schema (tables, constraints, views, append-only triggers) |
| `sql/seed_data.sql` | `INSERT` statements for every row, parents first, one transaction, foreign keys checked at commit |
| `db/liquidity.sqlite` | The schema with the data already loaded |
| `derived/` | Consolidated positions and forecast-accuracy reports (computed from the valid baseline) |
| `ground_truth/` | Labelled anomalies. Never inside `input/`, `json/`, `sql/` or `db/` |
| `run_config.json`, `generator_command.txt` | Configuration, its fingerprint and the exact command |
| `source_manifest.json` / `.md` | All data is synthetic, no external sources; table row counts; size and SHA-256 of every file above |

Rebuild a database from the SQL files alone: `sqlite3 liquidity.db < sql/schema.sql && sqlite3 liquidity.db < sql/seed_data.sql`.

## Reproducibility check

`python -m src.reproduce --scale starter|full [--seed N] [--report-dir DIR]`

1. Generates the dataset twice from the same seed, in two separate Python processes and fresh directories.
2. Compares the file lists, the SHA-256 of every file and the row count of every table in CSV, JSON, SQL seed (loaded into SQLite) and SQLite.
3. Generates once more with seed+1 and requires the data files to differ (sensitivity control).
4. Prints PASS/FAIL per comparison, writes `reproducibility_report.json` and `.md`, exits 1 on any failure.

Scope of the guarantee: identical bytes for the same command in the same environment (Python, numpy, pandas and SQLite versions are recorded in the manifest; numpy and pandas are pinned in `requirements.txt`). Nothing reads the clock, the host name, the working directory or global random state, and no output depends on set or dict ordering of strings (see design decision 61).
