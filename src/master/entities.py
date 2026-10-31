"""LegalEntity generation. Distributions are listed in docs/distributions.md.

Draw order from the ``master`` stream (kept fixed so adding an attribute later
does not reshuffle earlier ones): country permutation, type permutation,
parent choices, ages, status picks, closure dates, buffers, names.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd

from ..config import Config
from ..ids import IdFactory
from . import params as P

COLUMNS = ["entity_id", "legal_name", "country_code", "functional_currency", "entity_type",
           "parent_entity_id", "min_liquidity_buffer_base_minor", "status",
           "effective_from", "effective_to", "suspended_from"]


def _pick_countries(cfg: Config, rng: np.random.Generator) -> list[str]:
    available = {c: v for c, v in P.COUNTRIES.items() if v[0] in cfg.currency_codes}
    if not available:
        raise ValueError("no entity countries match the configured currencies")
    codes = list(available)
    weights = np.array([available[c][1] for c in codes], dtype=float)
    weights /= weights.sum()

    n = cfg.n_entities
    chosen: list[str] = []
    # stratify: one country per configured currency first, so each currency is used
    for currency in cfg.currency_codes:
        options = [c for c in codes if available[c][0] == currency]
        if not options or len(chosen) >= n:
            continue
        w = np.array([available[c][1] for c in options], dtype=float)
        chosen.append(options[int(rng.choice(len(options), p=w / w.sum()))])
    while len(chosen) < n:
        chosen.append(codes[int(rng.choice(len(codes), p=weights))])
    order = rng.permutation(n)
    return [chosen[i] for i in order]


def _entity_types(n: int, rng: np.random.Generator) -> list[str]:
    if n == 1:
        types = ["HOLDING"]
    elif n == 2:
        types = ["HOLDING", "OPERATING"]
    else:
        n_tc = max(1, round(P.TREASURY_CENTRE_SHARE * n))
        types = ["HOLDING"] + ["TREASURY_CENTRE"] * n_tc + ["OPERATING"] * (n - 1 - n_tc)
    order = rng.permutation(n)
    return [types[i] for i in order]


def _invented_name(rng: np.random.Generator, used: set[str], entity_type: str, country: str) -> str:
    suffix = P.LEGAL_SUFFIX.get(country, "Ltd")
    for _ in range(1000):
        k = int(rng.integers(P.NAME_STEM_SYLLABLES[0], P.NAME_STEM_SYLLABLES[1] + 1))
        stem = "".join(P.NAME_SYLLABLES[int(i)] for i in rng.integers(0, len(P.NAME_SYLLABLES), k)).capitalize()
        if entity_type == "HOLDING":
            core = f"{stem} Group Holdings"
        elif entity_type == "TREASURY_CENTRE":
            core = f"{stem} Treasury Centre"
        else:
            core = f"{stem} {P.INDUSTRY_WORDS[int(rng.integers(0, len(P.INDUSTRY_WORDS)))]}"
        name = f"{core} {suffix}"
        if name not in used:
            used.add(name)
            return name
    raise ValueError("could not generate a unique entity name")


def generate_legal_entities(cfg: Config, rng: np.random.Generator, ids: IdFactory) -> pd.DataFrame:
    n = cfg.n_entities
    countries = _pick_countries(cfg, rng)
    types = _entity_types(n, rng)
    currency_of = {c: P.COUNTRIES[c][0] for c in set(countries)}

    entity_ids = [ids.entity() for _ in range(n)]
    holding = types.index("HOLDING")
    treasury = [i for i, t in enumerate(types) if t == "TREASURY_CENTRE"]
    hubs = [holding] + treasury

    # parents: holding has none, treasury centres hang off the holding,
    # operating entities hang off the holding or a treasury centre (uniform)
    parent: list[int | None] = [None] * n
    for i, t in enumerate(types):
        if t == "TREASURY_CENTRE":
            parent[i] = holding
        elif t == "OPERATING":
            parent[i] = hubs[int(rng.integers(0, len(hubs)))]

    ages = rng.integers(P.ENTITY_AGE_DAYS[0], P.ENTITY_AGE_DAYS[1] + 1, n)
    effective_from: list = [None] * n
    for i in [holding] + treasury + [j for j, t in enumerate(types) if t == "OPERATING"]:
        own = cfg.date_start - timedelta(days=int(ages[i]))
        effective_from[i] = own if parent[i] is None else max(own, effective_from[parent[i]])

    # status is a snapshot as of date_end; quotas are stratified so small datasets
    # still contain every lifecycle state
    status = ["ACTIVE"] * n
    effective_to: list = [None] * n
    operating = [i for i, t in enumerate(types) if t == "OPERATING"]
    if n >= P.MIN_ENTITIES_FOR_STATUS_QUOTAS and operating:
        k_susp = min(len(operating), max(1, round(P.SUSPENDED_ENTITY_SHARE * n)))
        k_closed = min(len(operating) - k_susp, max(1, round(P.CLOSED_ENTITY_SHARE * n)))
        picks = [operating[int(i)] for i in rng.permutation(len(operating))]
        for i in picks[:k_susp]:
            status[i] = "SUSPENDED"
        for i in picks[k_susp:k_susp + k_closed]:
            status[i] = "CLOSED"
    closure_fracs = rng.uniform(P.CLOSURE_WINDOW_FRACTION[0], P.CLOSURE_WINDOW_FRACTION[1], n)
    for i in range(n):
        if status[i] == "CLOSED":
            effective_to[i] = cfg.date_start + timedelta(days=int(closure_fracs[i] * (cfg.n_days - 1)))

    suspension_fracs = rng.uniform(P.SUSPENSION_WINDOW_FRACTION[0], P.SUSPENSION_WINDOW_FRACTION[1], n)
    suspended_from: list = [None] * n
    for i in range(n):
        if status[i] == "SUSPENDED":
            suspended_from[i] = cfg.date_start + timedelta(days=int(suspension_fracs[i] * (cfg.n_days - 1)))

    z = rng.standard_normal(n)
    buffer_usd = np.exp(np.log(P.BUFFER_MEDIAN_USD) + P.BUFFER_SIGMA * z)
    buffer_minor = [int(np.rint(buffer_usd[i] * P.BUFFER_MULTIPLIER[types[i]] * 100)) for i in range(n)]

    used: set[str] = set()
    names = [_invented_name(rng, used, types[i], countries[i]) for i in range(n)]

    return pd.DataFrame({
        "entity_id": entity_ids,
        "legal_name": names,
        "country_code": countries,
        "functional_currency": [currency_of[c] for c in countries],
        "entity_type": types,
        "parent_entity_id": [None if parent[i] is None else entity_ids[parent[i]] for i in range(n)],
        "min_liquidity_buffer_base_minor": buffer_minor,
        "status": status,
        "effective_from": [d.isoformat() for d in effective_from],
        "effective_to": [None if d is None else d.isoformat() for d in effective_to],
        "suspended_from": [None if d is None else d.isoformat() for d in suspended_from],
    }, columns=COLUMNS)
