# Reproducibility report

Command: `python -m src.generate --scale full --seed 20261022`

**Result: PASS.** Two independent runs, 47 files compared.

* Files only in one run: 0
* SHA-256 mismatches: 0
* Row-count mismatches between runs: 0
* Tables whose CSV / JSON / SQL seed / SQLite row counts disagree: 0
* Sensitivity control (seed 20261023): 32 of 36 data files differ, as they must: PASS

## Rows per table

| Table | CSV | JSON | SQL seed | SQLite |
|---|---|---|---|---|
| bank_account | 150 | 150 | 150 | 150 |
| cash_flow_forecast | 20000 | 20000 | 20000 | 20000 |
| currency | 5 | 5 | 5 | 5 |
| currency_rate | 1460 | 1460 | 1460 | 1460 |
| daily_cash_position | 52760 | 52760 | 52760 | 52760 |
| forecast_daily_projection | 15210 | 15210 | 15210 | 15210 |
| forecast_run | 17 | 17 | 17 | 17 |
| forecast_run_status_event | 67 | 67 | 67 | 67 |
| forecast_vs_actual | 88040 | 88040 | 88040 | 88040 |
| ledger_entry | 100000 | 100000 | 100000 | 100000 |
| legal_entity | 30 | 30 | 30 | 30 |
| liquidity_scenario | 3 | 3 | 3 | 3 |
| payable | 22320 | 22320 | 22320 | 22320 |
| payable_status_event | 51282 | 51282 | 51282 | 51282 |
| receivable | 26520 | 26520 | 26520 | 26520 |
| receivable_status_event | 66335 | 66335 | 66335 | 66335 |
| settlement_link | 46913 | 46913 | 46913 | 46913 |

## SHA-256 (identical in both runs)

| File | SHA-256 |
|---|---|
| db/liquidity.sqlite | `4b2fd82fa636cd50bf2449dca9de739104fddf42342aaec54fb4306ae333fea1` |
| derived/consolidated_daily_position.csv | `15a8c27d694b4a079bdd3bfa8f70de9cab606369e09f89ae921d3ccbc33cd4cd` |
| derived/forecast_accuracy_by_run.csv | `0bf045989175ff83a572123aa635c85bfd4d9d4bf413ec162c2b8698b2670137` |
| derived/forecast_accuracy_by_scenario.csv | `7db974940d6d788f243442e92b3a7338e9f62c318f8d001e62d312d0af022746` |
| derived/forecast_accuracy_by_scenario.json | `4848efd12ab23d988c6cbd8dc079784544d8fa71e61e1e929d315c0c80320fec` |
| derived/forecast_variance_daily.csv | `c62c5b45f53895a0e40395bfc96f98ab66126e3d393e8084f0ade65168ff9885` |
| derived/variance_driver_summary.csv | `842d52a96590c6c6695ce3a27bed9091df3d079efef5426dbccaf1fc1e51576e` |
| generator_command.txt | `db2371b9613d6caf8e7c38bf39645e2fe0affa8d233b845e72a18768be44a104` |
| ground_truth/ground_truth.csv | `5c56ed48c53167e0667f877976456ed0f237cb22251982b1bee9b2ad8b839625` |
| ground_truth/ground_truth.json | `61f226cc2995f36de9af6321e8b5a602675ff6dcb45a12b38b47d557ee1c9852` |
| input/bank_account.csv | `1acd42f0871c8e58017e32865f4d73d4b755c1269a980b60f98286c374bf65dd` |
| input/cash_flow_forecast.csv | `27e673ae4fe3d287b100d881caaf7fffd9ac44f9dd2f52a18acb956ca0f501b9` |
| input/currency.csv | `86a76221ab9a6b2e3642d31cfb8ba88391ff51a30f4bd3cb10ecb6b4e04baf38` |
| input/currency_rate.csv | `9413d22093986accad1b2d3ef305e8fdb3f7515ac6111b4ce0dafd0da39b1af4` |
| input/daily_cash_position.csv | `4bb62602f70273cf8c5b78ba2e19a4688c387c65d25487170355c41d93b0faa1` |
| input/forecast_daily_projection.csv | `9c476125d0e37c86e54689f206a3897aab60d7239a04e022a7a660d3a3306cc7` |
| input/forecast_run.csv | `6c1e5b8610f3471c0111180b3a13cfa84a9d9d3fbb4a348071f641a7940fa145` |
| input/forecast_run_status_event.csv | `be2167ac446fe4878be1a4216c03b259eaf38023db1067c641d2a6d473031a7d` |
| input/forecast_vs_actual.csv | `3927d73fd8c363c2874679374525306115f8ca6c5c993ead7bc1374e8e1a5f2e` |
| input/ledger_entry.csv | `848afd84e379aa6a860425debe6d535db632654d20c5fac672f98c4e92b266f6` |
| input/legal_entity.csv | `abc2403422930a9362cccce2b60361ee6d00c095b6b6f7cefcfa6e092c38fc4d` |
| input/liquidity_scenario.csv | `aad9835bb8e865651d0b66d34656a0be69da84689e56b9776daf43b2302b84cf` |
| input/payable.csv | `0231f39c1e24b3214cdd5b6075abc8442afe18ee546b7347a410392abd8de257` |
| input/payable_status_event.csv | `913663799e98cf8ba1f3c4a57a3f50a67a72d138ba703897254e5f22d6164e0b` |
| input/receivable.csv | `f27243f6e137b52acf81c576972935e24aa53bc891f31e1fc2f50f5e8071d8a5` |
| input/receivable_status_event.csv | `1e82250583db0bbe1609437026d4f7f82251593b074ad31991c85ee408c1fa81` |
| input/settlement_link.csv | `5c7ba95302e29f04b8c6e932163d731712966307163e95fb5ac372344731313e` |
| json/bank_account.json | `4be99cc6b5fb820ca94684d8a718acc00c3f6952ebd31271ba4cdf6b6ae2f7c0` |
| json/cash_flow_forecast.json | `471a857ff3afc4046e74dc256b09afd04cbc19b382db06fe89ecfd2afe46b003` |
| json/currency.json | `8edf57310e82845fcee7542b64dac263e8062e29058b5fb5c787fa94d7fa3490` |
| json/currency_rate.json | `afa86642157a76c7e3450bdcaf66b67af50f906439a48a7b32141f736b9592a8` |
| json/daily_cash_position.json | `373765c7518f9972c197291c1490084bbc38c63d327ef53b092b52820b43df22` |
| json/forecast_daily_projection.json | `38a656c647da5ce770f0c2b24ea164670f67bab1ee7220e814b4ffe634870eb7` |
| json/forecast_run.json | `dac1281fd682d7a74c73e2823e59c5773d2cc078a2bf46e54926396bc7c52555` |
| json/forecast_run_status_event.json | `8a84fa5af4b6345ee422fc7dbdaece182135c3f4c265ed9a1e56a05e24ef56ea` |
| json/forecast_vs_actual.json | `6bfcec8634506ddf7abeaa6b8e6730357ed94c37ca1398d876456c8ffe0a3c77` |
| json/ledger_entry.json | `60cc06373a5e8063a153dea899ab77da2ff33aed878ce0afb235af3450794174` |
| json/legal_entity.json | `c5fde48a95f0811b825b18f5bf26b3ad1088c2d49e1cc3e72f489411a210103b` |
| json/liquidity_scenario.json | `e27a9889cbf90347e2b7fb3f48e51f7bd0e946c522ce79f4d342d1d3089a0dcc` |
| json/payable.json | `c0f5207cf1a672a2cb9c265a2a87ad14a4d2a776a83817bb8f47ffaa6992cfa0` |
| json/payable_status_event.json | `10d0f6fd13646232cbc058a9cc6534a48428dc942e436d1a8f819985590cb666` |
| json/receivable.json | `446c7432208d35d2c6b06f99b748fdacf9b598f2e659a7dc0f2a9b198beec500` |
| json/receivable_status_event.json | `24284ba03484a65c603463f2ef30da3b53be197d298983b7cdfeff51f5fbefb3` |
| json/settlement_link.json | `46a16601777a2e5d37c9f3960aef7701350e26826c68e8c49664aaea734d4bee` |
| run_config.json | `d41f44c8938d6d0c6a12707910a9e33a3486a9b1e872a4c53c906df246269e17` |
| sql/schema.sql | `d05838487f913ce087f9a05314aa6be77afb058e8b747f03a131ae2a20da0cf9` |
| sql/seed_data.sql | `40c62adc55a4d27c2d9b5822f3a2e6d58b38fa5b7adf15ca465fdbe22fb98f15` |
