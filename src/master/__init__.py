"""Master (parent) tables, generated in dependency order.

CurrencyRate has no parents so it is generated first; LegalEntity next; BankAccount
last, because it needs both (entity FK and the opening-date FX rate used to size
opening balances).
"""
from __future__ import annotations

import pandas as pd

from ..config import Config
from ..ids import IdFactory
from ..rng import RngStreams
from .accounts import generate_bank_accounts
from .entities import generate_legal_entities
from .fx import generate_currency_rates, rate_lookup


def currency_table(cfg: Config) -> pd.DataFrame:
    return pd.DataFrame({
        "currency_code": [c.code for c in cfg.currencies],
        "currency_name": [c.name for c in cfg.currencies],
        "minor_unit_exponent": [c.minor_unit_exponent for c in cfg.currencies],
        "is_base": [int(c.is_base) for c in cfg.currencies],
    })


def generate_master(cfg: Config, streams: RngStreams | None = None,
                    ids: IdFactory | None = None) -> dict[str, pd.DataFrame]:
    """Return {table name: DataFrame} for currency, currency_rate, legal_entity, bank_account,
    in an order that satisfies every foreign key."""
    streams = streams or RngStreams(cfg.seed)
    ids = ids or IdFactory()

    currency = currency_table(cfg)
    rates = generate_currency_rates(cfg, streams.fx)
    entities = generate_legal_entities(cfg, streams.master, ids)
    accounts = generate_bank_accounts(cfg, streams.master, ids, entities,
                                      rate_lookup(rates, cfg.base_currency.code))
    return {"currency": currency, "currency_rate": rates,
            "legal_entity": entities, "bank_account": accounts}
