"""Master data: key uniqueness, FK validity, cross-row rules, distributions, determinism."""
import hashlib
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.config import build_config
from src.db import create_database, load_tables
from src.export import write_csv_tables
from src.master import generate_master
from src.master import params as P
from src.master.fx import rate_lookup
from src.rng import RngStreams

ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------------------
# fixtures: generate each scale once
# ----------------------------------------------------------------------------
@pytest.fixture(scope="module", params=["starter", "full"])
def built(request):
    cfg = build_config(request.param)
    return cfg, generate_master(cfg)


@pytest.fixture(scope="module")
def full():
    cfg = build_config("full")
    return cfg, generate_master(cfg)


def iso(d):
    return d.isoformat()


# ----------------------------------------------------------------------------
# key uniqueness
# ----------------------------------------------------------------------------
PRIMARY_KEYS = {"currency": ["currency_code"], "legal_entity": ["entity_id"],
                "bank_account": ["bank_account_id"], "currency_rate": ["rate_id"]}


def test_primary_keys_are_unique_and_never_null(built):
    _, t = built
    for table, cols in PRIMARY_KEYS.items():
        assert not t[table][cols].isna().any().any(), table
        assert not t[table].duplicated(cols).any(), table


def test_natural_keys_are_unique(built):
    _, t = built
    assert t["legal_entity"]["legal_name"].is_unique
    assert not t["bank_account"].duplicated(["entity_id", "account_seq"]).any()
    assert not t["currency_rate"].duplicated(["quote_currency", "base_currency", "rate_date"]).any()


def test_entity_ids_are_sequential_from_one(built):
    cfg, t = built
    assert t["legal_entity"]["entity_id"].tolist() == [f"ENT-{i:06d}" for i in range(1, cfg.n_entities + 1)]


def test_account_ids_encode_entity_and_sequence_and_sequences_are_gap_free(built):
    _, t = built
    acc = t["bank_account"]
    for row in acc.itertuples():
        assert row.bank_account_id == f"BANKACC-{row.entity_id}-{row.account_seq:03d}"
    for _, grp in acc.groupby("entity_id"):
        assert sorted(grp["account_seq"]) == list(range(1, len(grp) + 1))


def test_rate_ids_encode_currency_and_date(built):
    _, t = built
    r = t["currency_rate"]
    expected = "FXR-" + r["quote_currency"] + "-" + r["rate_date"].str.replace("-", "", regex=False)
    assert (r["rate_id"] == expected).all()


# ----------------------------------------------------------------------------
# foreign keys
# ----------------------------------------------------------------------------
def test_tables_load_into_the_schema_with_foreign_keys_enforced(built):
    _, t = built
    conn = create_database()
    load_tables(conn, t)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    for table in t:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == len(t[table])


def test_foreign_keys_hold_in_pandas_too(built):
    _, t = built
    ent_ids, codes = set(t["legal_entity"]["entity_id"]), set(t["currency"]["currency_code"])
    assert set(t["bank_account"]["entity_id"]) <= ent_ids
    assert set(t["legal_entity"]["parent_entity_id"].dropna()) <= ent_ids
    assert set(t["bank_account"]["currency"]) <= codes
    assert set(t["legal_entity"]["functional_currency"]) <= codes
    assert set(t["currency_rate"]["quote_currency"]) <= codes
    base = t["currency"].loc[t["currency"]["is_base"] == 1, "currency_code"].tolist()
    assert base == ["USD"] and set(t["currency_rate"]["base_currency"]) == set(base)


def test_a_broken_foreign_key_is_rejected_and_nothing_is_kept(built):
    _, t = built
    bad = {k: v.copy() for k, v in t.items()}
    bad["bank_account"].loc[0, "entity_id"] = "ENT-999999"
    conn = create_database()
    with pytest.raises(sqlite3.IntegrityError):
        load_tables(conn, bad)
    assert conn.execute("SELECT COUNT(*) FROM legal_entity").fetchone()[0] == 0


def test_every_entity_has_at_least_one_account(built):
    _, t = built
    assert set(t["bank_account"]["entity_id"]) == set(t["legal_entity"]["entity_id"])


# ----------------------------------------------------------------------------
# volume
# ----------------------------------------------------------------------------
def test_row_counts_match_the_config(built):
    cfg, t = built
    assert len(t["legal_entity"]) == cfg.n_entities
    assert len(t["bank_account"]) == cfg.n_accounts
    assert len(t["currency"]) == 5
    assert len(t["currency_rate"]) == cfg.n_days * (len(cfg.currencies) - 1)


def test_full_scale_matches_the_brief(full):
    _, t = full
    assert (len(t["legal_entity"]), len(t["bank_account"])) == (30, 150)
    assert set(t["bank_account"]["currency"]) == {"USD", "EUR", "GBP", "INR", "JPY"}


def test_every_currency_is_a_functional_currency_somewhere(built):
    _, t = built
    assert set(t["legal_entity"]["functional_currency"]) == {"USD", "EUR", "GBP", "INR", "JPY"}


# ----------------------------------------------------------------------------
# entity rules
# ----------------------------------------------------------------------------
def test_group_hierarchy_is_a_tree_rooted_at_one_holding(built):
    _, t = built
    e = t["legal_entity"].set_index("entity_id")
    holdings = e.index[e["entity_type"] == "HOLDING"].tolist()
    assert len(holdings) == 1 and pd.isna(e.loc[holdings[0], "parent_entity_id"])
    assert e["parent_entity_id"].drop(holdings[0]).notna().all()
    for eid in e.index:                              # walking up always reaches the holding
        seen, cur = set(), eid
        while not pd.isna(e.loc[cur, "parent_entity_id"]):
            assert cur not in seen
            seen.add(cur)
            cur = e.loc[cur, "parent_entity_id"]
        assert cur == holdings[0]
    for eid, row in e.iterrows():
        if row["entity_type"] == "TREASURY_CENTRE":
            assert row["parent_entity_id"] == holdings[0]
        if row["entity_type"] == "OPERATING":
            assert e.loc[row["parent_entity_id"], "entity_type"] in {"HOLDING", "TREASURY_CENTRE"}


def test_a_child_never_starts_before_its_parent(built):
    _, t = built
    e = t["legal_entity"].set_index("entity_id")
    for eid, row in e.dropna(subset=["parent_entity_id"]).iterrows():
        assert row["effective_from"] >= e.loc[row["parent_entity_id"], "effective_from"]


def test_entities_predate_the_data_window(built):
    cfg, t = built
    assert (t["legal_entity"]["effective_from"] < iso(cfg.date_start)).all()


def test_entity_lifecycle_states_are_all_present_and_consistent(built):
    cfg, t = built
    e = t["legal_entity"]
    assert set(e["status"]) == {"ACTIVE", "SUSPENDED", "CLOSED"}
    assert ((e["status"] == "CLOSED") == e["effective_to"].notna()).all()
    closed = e[e["status"] == "CLOSED"]
    lo = cfg.date_start + timedelta(days=int(0.75 * (cfg.n_days - 1)))
    hi = cfg.date_start + timedelta(days=int(0.97 * (cfg.n_days - 1)))
    assert closed["effective_to"].between(iso(lo), iso(hi)).all()
    assert (closed["effective_to"] > closed["effective_from"]).all()
    # the group backbone never closes or suspends
    assert (e.loc[e["entity_type"] != "OPERATING", "status"] == "ACTIVE").all()


def test_country_and_currency_agree(built):
    _, t = built
    e = t["legal_entity"]
    for row in e.itertuples():
        assert P.COUNTRIES[row.country_code][0] == row.functional_currency


def test_names_look_invented_not_empty_and_carry_the_country_suffix(built):
    _, t = built
    for row in t["legal_entity"].itertuples():
        assert row.legal_name.endswith(P.LEGAL_SUFFIX[row.country_code])
        assert re.fullmatch(r"[A-Z][a-z]+ [A-Za-z .&]+", row.legal_name)


# ----------------------------------------------------------------------------
# account rules
# ----------------------------------------------------------------------------
def test_primary_account_is_an_active_operating_account_in_the_functional_currency(built):
    cfg, t = built
    acc = t["bank_account"].merge(t["legal_entity"], on="entity_id", suffixes=("", "_ent"))
    prim = acc[acc["account_seq"] == 1]
    assert (prim["account_type"] == "OPERATING").all()
    assert (prim["currency"] == prim["functional_currency"]).all()
    assert (prim["opening_date"] == iso(cfg.date_start)).all()
    open_entities = prim["status_ent"] != "CLOSED"
    assert (prim.loc[open_entities, "status"] == "ACTIVE").all()


def test_account_dates_sit_inside_the_window_and_the_entity_window(built):
    cfg, t = built
    acc = t["bank_account"].merge(t["legal_entity"], on="entity_id", suffixes=("", "_ent"))
    latest_open = cfg.date_start + timedelta(days=int(P.NEW_ACCOUNT_OPEN_FRACTION[1] * (cfg.n_days - 1)))
    assert acc["opening_date"].between(iso(cfg.date_start), iso(latest_open)).all()
    assert (acc["effective_from"] <= acc["opening_date"]).all()
    assert (acc["effective_from"] >= acc["effective_from_ent"]).all()
    closed = acc[acc["effective_to"].notna()]
    assert (closed["opening_date"] < closed["effective_to"]).all()
    entity_closed = acc[acc["effective_to_ent"].notna()]
    assert (entity_closed["status"] == "CLOSED").all()
    assert (entity_closed["effective_to"] == entity_closed["effective_to_ent"]).all()
    # an account never outlives its entity
    both = acc[acc["effective_to"].notna() & acc["effective_to_ent"].notna()]
    assert (both["effective_to"] <= both["effective_to_ent"]).all()


def test_account_lifecycle_states_are_all_present_and_consistent(built):
    _, t = built
    a = t["bank_account"]
    assert set(a["status"]) == {"ACTIVE", "DORMANT", "CLOSED"}
    assert ((a["status"] == "CLOSED") == a["effective_to"].notna()).all()


def test_opening_balances_are_nonnegative_integers_and_include_the_zero_boundary(full):
    _, t = full
    b = t["bank_account"]["opening_balance_minor"]
    assert (b >= 0).all() and str(b.dtype).startswith("int")
    assert (b == 0).any()
    assert (b > 0).sum() > 0.9 * len(b)


def test_brand_new_accounts_start_smaller_than_mature_ones(full):
    cfg, t = full
    acc = t["bank_account"]
    rates = rate_lookup(t["currency_rate"], "USD")
    exp = t["currency"].set_index("currency_code")["minor_unit_exponent"].to_dict()
    usd = acc.apply(lambda r: r["opening_balance_minor"] / 10 ** exp[r["currency"]]
                    * rates.at[r["opening_date"], r["currency"]], axis=1)
    new, old = usd[acc["opening_date"] > iso(cfg.date_start)], usd[acc["opening_date"] == iso(cfg.date_start)]
    assert len(new) >= 5
    assert new.median() < 0.5 * old.median()


# ----------------------------------------------------------------------------
# distribution sanity (loose bounds: these catch a broken generator, not noise)
# ----------------------------------------------------------------------------
def test_accounts_per_entity_average_five(full):
    _, t = full
    counts = t["bank_account"].groupby("entity_id").size()
    assert counts.min() >= 1 and counts.mean() == pytest.approx(5.0) and counts.max() <= 12


def test_share_of_non_primary_accounts_in_the_functional_currency(full):
    _, t = full
    acc = t["bank_account"].merge(t["legal_entity"], on="entity_id")
    rest = acc[acc["account_seq"] > 1]
    share = (rest["currency"] == rest["functional_currency"]).mean()
    assert 0.5 <= share <= 0.8        # parameter is 0.65


def test_opening_balance_spread_matches_the_lognormal_parameters(full):
    cfg, t = full
    acc = t["bank_account"]
    rates = rate_lookup(t["currency_rate"], "USD")
    exp = t["currency"].set_index("currency_code")["minor_unit_exponent"].to_dict()
    mature = acc[(acc["opening_date"] == iso(cfg.date_start)) & (acc["opening_balance_minor"] > 0)]
    z = mature.apply(lambda r: np.log(r["opening_balance_minor"] / 10 ** exp[r["currency"]]
                                      * rates.at[r["opening_date"], r["currency"]]
                                      / P.OPENING_BALANCE_MEDIAN_USD[r["account_type"]]), axis=1)
    assert abs(z.mean()) < 0.25                       # centred on the stated medians
    assert 0.65 <= z.std() <= 1.2                     # sigma parameter is 0.9


def test_entity_buffer_distribution(full):
    _, t = full
    e = t["legal_entity"]
    usd = e["min_liquidity_buffer_base_minor"] / 100
    assert (usd > 0).all()
    op = usd[e["entity_type"] == "OPERATING"]
    assert P.BUFFER_MEDIAN_USD / 3 < op.median() < P.BUFFER_MEDIAN_USD * 3
    hub = usd[e["entity_type"] != "OPERATING"]
    assert hub.median() > op.median()                 # holding / treasury multiplier


# ----------------------------------------------------------------------------
# FX
# ----------------------------------------------------------------------------
def weekday(s):
    return date.fromisoformat(s).weekday() < 5


def test_rate_grid_is_complete_and_contiguous(built):
    cfg, t = built
    r = t["currency_rate"]
    days = [iso(cfg.date_start + timedelta(days=i)) for i in range(cfg.n_days)]
    for q in ("EUR", "GBP", "INR", "JPY"):
        assert r.loc[r["quote_currency"] == q, "rate_date"].tolist() == days


def test_rates_are_positive_rounded_and_start_at_the_documented_levels(built):
    cfg, t = built
    r = t["currency_rate"]
    assert (r["rate"] > 0).all()
    assert np.allclose(r["rate"] * 1e8, np.round(r["rate"] * 1e8), atol=1e-4)
    first = r[r["rate_date"] == iso(cfg.date_start)].set_index("quote_currency")["rate"]
    for q, level in {"EUR": 1.08, "GBP": 1.27, "INR": 0.0120, "JPY": 0.0067}.items():
        assert first[q] == pytest.approx(level)


def test_weekend_rates_repeat_friday(built):
    _, t = built
    wide = rate_lookup(t["currency_rate"], "USD")
    dates = list(wide.index)
    for i in range(1, len(dates)):
        if not weekday(dates[i]):
            assert (wide.iloc[i][["EUR", "GBP", "INR", "JPY"]] == wide.iloc[i - 1][["EUR", "GBP", "INR", "JPY"]]).all()


def test_levels_stay_in_a_plausible_band(full):
    _, t = full
    wide = rate_lookup(t["currency_rate"], "USD")
    for q, level in P.INITIAL_RATE.items():
        assert wide[q].between(0.75 * level, 1.30 * level).all(), q


def shock_dates(rates):
    flagged = rates[rates["rate_source"] == "SYNTHETIC_SHOCK"]
    return flagged, sorted(flagged["rate_date"].unique())


def test_shock_days_follow_the_documented_rules(built):
    cfg, t = built
    flagged, dates = shock_dates(t["currency_rate"])
    assert len(dates) == max(1, round(cfg.n_days / P.SHOCK_EVERY_DAYS))
    assert all(weekday(d) for d in dates)
    idx = [(date.fromisoformat(d) - cfg.date_start).days for d in dates]
    assert all(P.SHOCK_EDGE_DAYS <= i < cfg.n_days - P.SHOCK_EDGE_DAYS for i in idx)
    assert all(b - a >= P.SHOCK_MIN_SPACING_DAYS for a, b in zip(idx, idx[1:]))
    # market-wide: every non-base currency is flagged on every shock date
    assert flagged.groupby("rate_date")["quote_currency"].nunique().eq(4).all()
    assert (t["currency_rate"]["rate_source"].isin({"SYNTHETIC_SHOCK", "SYNTHETIC_RANDOM_WALK"})).all()


def test_shock_moves_have_the_documented_direction_and_dwarf_ordinary_days(full):
    _, t = full
    wide = rate_lookup(t["currency_rate"], "USD")
    ret = np.log(wide[["EUR", "GBP", "INR", "JPY"]]).diff().iloc[1:]
    _, dates = shock_dates(t["currency_rate"])
    ordinary_days = [d for d in ret.index if d not in dates and weekday(d)]
    documented = {"EUR": -1, "GBP": -1, "INR": -1, "JPY": +1}   # docs/distributions.md, written out on purpose
    for q in ret.columns:
        moves = ret.loc[dates, q]
        assert (np.sign(moves) == documented[q]).all(), q
        assert moves.abs().mean() > 3 * ret.loc[ordinary_days, q].abs().mean(), q


def test_ordinary_daily_volatility_matches_the_parameters(full):
    _, t = full
    wide = rate_lookup(t["currency_rate"], "USD")
    ret = np.log(wide[["EUR", "GBP", "INR", "JPY"]]).diff().iloc[1:]
    _, dates = shock_dates(t["currency_rate"])
    ordinary = ret.loc[[d for d in ret.index if d not in dates and weekday(d)]]
    for q in ordinary.columns:
        ratio = ordinary[q].std() / P.DAILY_VOL[q]
        assert 0.7 <= ratio <= 1.4, (q, ratio)       # a little above 1: post-shock vol boost
        assert ordinary[q].abs().max() < 0.08, q


# ----------------------------------------------------------------------------
# determinism and stream independence
# ----------------------------------------------------------------------------
def test_same_seed_gives_identical_tables(built):
    cfg, t = built
    again = generate_master(cfg)
    for name in t:
        pd.testing.assert_frame_equal(t[name], again[name])


def test_a_different_seed_changes_every_random_table():
    a, b = generate_master(build_config("starter")), generate_master(build_config("starter", seed=1))
    for name in ("legal_entity", "bank_account", "currency_rate"):
        assert not a[name].equals(b[name]), name
    pd.testing.assert_frame_equal(a["currency"], b["currency"])   # not random


def test_csv_output_is_byte_identical_and_checksums_match(tmp_path, built):
    cfg, t = built
    p1 = write_csv_tables(t, tmp_path / "a")
    p2 = write_csv_tables(generate_master(cfg), tmp_path / "b")
    for x, y in zip(p1, p2):
        assert hashlib.sha256(x.read_bytes()).hexdigest() == hashlib.sha256(y.read_bytes()).hexdigest()
    assert b"\r" not in p1[0].read_bytes()


def test_fx_draws_do_not_change_entities_or_account_structure():
    """The fx stream is separate: burning it changes rates (and so balances) only."""
    cfg = build_config("starter")
    base = generate_master(cfg)
    streams = RngStreams(cfg.seed)
    streams.fx.random(12345)
    burned = generate_master(cfg, streams=streams)
    assert not base["currency_rate"].equals(burned["currency_rate"])
    pd.testing.assert_frame_equal(base["legal_entity"], burned["legal_entity"])
    cols = [c for c in base["bank_account"].columns if c != "opening_balance_minor"]
    pd.testing.assert_frame_equal(base["bank_account"][cols], burned["bank_account"][cols])


# ----------------------------------------------------------------------------
# edge configurations and documentation
# ----------------------------------------------------------------------------
@pytest.mark.parametrize("n_entities,n_accounts", [(1, 1), (1, 4), (2, 2), (2, 7), (3, 3), (5, 40)])
def test_small_configs_still_produce_valid_loadable_data(n_entities, n_accounts):
    cfg = build_config("starter", n_entities=n_entities, n_accounts=n_accounts)
    t = generate_master(cfg)
    assert len(t["legal_entity"]) == n_entities and len(t["bank_account"]) == n_accounts
    conn = create_database()
    load_tables(conn, t)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_unknown_currency_without_fx_parameters_is_a_clear_error():
    from dataclasses import replace
    from src.config import CURRENCIES, Currency
    cfg = replace(build_config("starter"), currencies=CURRENCIES + (Currency("CHF", "Franc", 2),))
    with pytest.raises(ValueError, match="no FX parameters"):
        generate_master(cfg)


def test_every_parameter_is_documented():
    doc = (ROOT / "docs" / "distributions.md").read_text()
    names = [n for n in vars(P) if n.isupper()]
    assert names, "no parameters found"
    missing = [n for n in names if n not in doc]
    assert not missing, f"undocumented parameters: {missing}"
