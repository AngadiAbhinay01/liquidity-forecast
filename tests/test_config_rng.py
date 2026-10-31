import ast
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from src.config import CURRENCIES, DEFAULT_SEED, SCALES, build_config
from src.rng import STREAMS, RngStreams, make_rng

SRC = Path(__file__).resolve().parents[1] / "src"


# ----------------------------------------------------------------------------
# config
# ----------------------------------------------------------------------------
def test_full_scale_matches_the_brief():
    c = build_config("full")
    assert c.seed == DEFAULT_SEED == 20261022
    assert (c.n_entities, c.n_accounts, c.n_ledger, c.n_forecast_items) == (30, 150, 100_000, 20_000)
    assert len(c.currencies) == 5
    assert c.base_currency.code == "USD"
    assert c.currency_codes == ("USD", "EUR", "GBP", "INR", "JPY")


def test_starter_uses_the_same_currencies_and_calendar_as_full():
    full, starter = build_config("full"), build_config("starter")
    assert starter.currencies == full.currencies
    assert (starter.date_start, starter.date_end) == (full.date_start, full.date_end)
    assert starter.n_ledger < full.n_ledger and starter.n_entities < full.n_entities


def test_every_preset_passes_validation():
    for name in SCALES:
        build_config(name)


def test_currency_exponents_match_schema_seed_expectations():
    exp = {c.code: c.minor_unit_exponent for c in CURRENCIES}
    assert exp == {"USD": 2, "EUR": 2, "GBP": 2, "INR": 2, "JPY": 0}
    assert sum(c.is_base for c in CURRENCIES) == 1


def test_calendar_and_cutoff_arithmetic():
    c = build_config("full")
    assert c.n_days == 365
    assert c.last_cutoff_date == date(2026, 12, 1)  # 30-day horizon fits before 12-31


def test_overrides_replace_preset_values_and_none_is_ignored():
    c = build_config("starter", n_ledger=500, n_entities=None)
    assert c.n_ledger == 500
    assert c.n_entities == SCALES["starter"].n_entities


@pytest.mark.parametrize("overrides,fragment", [
    (dict(n_accounts=1), "n_accounts must be >= n_entities"),
    (dict(n_entities=0), "n_entities must be a positive integer"),
    (dict(n_ledger=-5), "n_ledger must be a positive integer"),
    (dict(n_forecast_items=True), "n_forecast_items"),
    (dict(n_accounts=30 * 999 + 1), "at most 999 accounts per entity"),
    (dict(date_end=date(2025, 12, 31)), "date_end must be after date_start"),
    (dict(forecast_horizon_days=0), "forecast_horizon_days"),
    (dict(forecast_horizon_days=400), "too short"),
    (dict(seed=-1), "seed"),
])
def test_invalid_configs_are_rejected_with_a_reason(overrides, fragment):
    with pytest.raises(ValueError, match=fragment):
        build_config("full", **overrides)


def test_currency_rules_are_validated():
    base = build_config("full")
    with pytest.raises(ValueError, match="exactly one currency"):
        replace(base, currencies=tuple(replace(c, is_base=False) for c in CURRENCIES)).validate()
    with pytest.raises(ValueError, match="exactly one currency"):
        replace(base, currencies=tuple(replace(c, is_base=True) for c in CURRENCIES)).validate()
    with pytest.raises(ValueError, match="duplicate"):
        replace(base, currencies=CURRENCIES + (CURRENCIES[1],)).validate()


def test_unknown_scale_is_rejected():
    with pytest.raises(ValueError, match="unknown scale"):
        build_config("huge")


def test_config_is_immutable():
    c = build_config("starter")
    with pytest.raises(Exception):
        c.seed = 1  # frozen dataclass


def test_to_dict_is_json_safe_and_fingerprint_is_stable_and_sensitive():
    c = build_config("full")
    json.dumps(c.to_dict())
    assert c.fingerprint() == build_config("full").fingerprint()
    assert c.fingerprint() != build_config("full", seed=1).fingerprint()
    assert c.fingerprint() != build_config("starter").fingerprint()
    assert c.fingerprint() != build_config("full", n_ledger=99_999).fingerprint()


# ----------------------------------------------------------------------------
# rng
# ----------------------------------------------------------------------------
def draw(rng):
    return rng.integers(0, 1_000_000, 8).tolist()


def test_golden_values_catch_a_numpy_or_stream_change():
    """Pinned numpy 2.5.3. If this fails after an upgrade, regenerate every
    reproducibility checksum on purpose, not by accident."""
    assert make_rng(20261022, "master").integers(0, 1000, 6).tolist() == [235, 427, 986, 616, 809, 615]
    assert make_rng(20261022, "fx").integers(0, 1000, 6).tolist() == [976, 642, 411, 663, 839, 819]


def test_same_seed_and_stream_repeat_exactly():
    assert draw(make_rng(7, "txn")) == draw(make_rng(7, "txn"))


def test_streams_differ_from_each_other_and_across_seeds():
    values = {tuple(draw(make_rng(7, s))) for s in STREAMS}
    assert len(values) == len(STREAMS)
    assert draw(make_rng(7, "txn")) != draw(make_rng(8, "txn"))


def test_a_stream_is_unaffected_by_what_other_streams_did_first():
    clean = draw(make_rng(7, "forecast"))
    streams = RngStreams(7)
    streams.master.random(10_000)      # heavy use of another stream first
    streams.anomaly.random(5)
    assert draw(streams.forecast) == clean


def test_streams_object_reuses_one_generator_per_name():
    s = RngStreams(7)
    assert s.get("txn") is s.txn
    first, second = draw(s.txn), draw(s.txn)
    assert first != second                       # it advances
    fresh = make_rng(7, "txn")
    assert first == draw(fresh) and second == draw(fresh)


def test_unknown_names_and_bad_seeds_are_rejected():
    with pytest.raises(ValueError):
        make_rng(7, "weather")
    for bad in (-1, 2**63, 1.5, "7", None, True):
        with pytest.raises(ValueError):
            make_rng(bad, "txn")
    with pytest.raises(AttributeError):
        RngStreams(7).weather


def test_numpy_global_random_state_is_never_touched():
    before = np.random.get_state()
    s = RngStreams(DEFAULT_SEED)
    for name in STREAMS:
        s.get(name).random(100)
    after = np.random.get_state()
    assert before[0] == after[0] and (before[1] == after[1]).all() and before[2:] == after[2:]


# ----------------------------------------------------------------------------
# guard: no hidden nondeterminism anywhere in src/
# ----------------------------------------------------------------------------
BANNED_IMPORTS = {"random", "time", "uuid", "secrets"}
BANNED_CALL_SUFFIXES = (
    "datetime.now", "datetime.utcnow", "datetime.today", "date.today",
    "time.time", "time.monotonic", "time.perf_counter", "os.urandom", "os.getpid",
)
ALLOWED_NP_RANDOM = {"Generator", "PCG64", "SeedSequence"}  # default_rng() is banned: unseeded by default


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def nondeterminism_findings(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if a.name.split(".")[0] in BANNED_IMPORTS]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in BANNED_IMPORTS:
                found.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Attribute):
            name = _dotted(node)
            if not name:
                continue
            if name.endswith(BANNED_CALL_SUFFIXES):
                found.append(name)
            for prefix in ("np.random.", "numpy.random."):
                if name.startswith(prefix) and name[len(prefix):].split(".")[0] not in ALLOWED_NP_RANDOM:
                    found.append(name)
    return found


def test_the_scanner_itself_detects_violations():
    bad = (
        "import random\nimport time\nfrom uuid import uuid4\nimport numpy as np\n"
        "from datetime import datetime\n"
        "np.random.seed(1)\nx = np.random.rand(3)\ng = np.random.default_rng()\n"
        "t = datetime.now()\nu = time.time()\n"
    )
    found = nondeterminism_findings(bad)
    for expected in ("import random", "import time", "from uuid import ...", "np.random.seed",
                     "np.random.rand", "np.random.default_rng", "datetime.now", "time.time"):
        assert expected in found, (expected, found)


def test_scanner_ignores_docstrings_and_the_allowed_numpy_api():
    ok = '"""mentions random, np.random.seed and datetime.now() in prose"""\n' \
         "import numpy as np\ng = np.random.Generator(np.random.PCG64(np.random.SeedSequence(1)))\n"
    assert nondeterminism_findings(ok) == []


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_src_contains_no_clock_global_rng_or_unseeded_randomness(path):
    assert nondeterminism_findings(path.read_text()) == []
