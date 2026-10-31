"""Deterministic, immutable identifiers.

Formats (the first five are mandated by the brief; the rest are extensions
documented in docs/data_dictionary.md):

    LegalEntity       ENT-000001
    BankAccount       BANKACC-ENT-000001-001       (BANKACC-<entity-id>-nnn)
    LedgerEntry       LED-2026-00000001            (year of entry_date, counter per year)
    CashFlowForecast  CF-2026-000001               (year of run cutoff, counter per year)
    ForecastRun       FCAST-2026-01-01-001         (cutoff date, counter per date)
    Receivable        RCV-2026-000001              (year of invoice_date, counter per year)
    Payable           PAY-2026-000001              (year of invoice_date, counter per year)
    CurrencyRate      FXR-EUR-20260105
    LiquidityScenario SCN-001
    ForecastVsActual  VAR-2026-000001

The pure ``format_*`` functions are stateless. ``IdFactory`` holds the counters.
A counter only ever moves forward, so an ID is never reused or changed.
Nothing here reads the clock or any random source: the same calls in the same
order always produce the same IDs.

The formats are validated by the CHECK constraints in sql/schema.sql;
tests/test_ids.py loads helper output into that schema to keep the two in step.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Union

DateLike = Union[date, str]

# Widths of the zero-padded counters.
ENTITY_WIDTH = 6
ACCOUNT_WIDTH = 3
LEDGER_WIDTH = 8
CASH_FLOW_WIDTH = 6
RUN_WIDTH = 3
RECEIVABLE_WIDTH = 6
PAYABLE_WIDTH = 6
SCENARIO_WIDTH = 3
COMPARISON_WIDTH = 6

D = "[0-9]"
ID_PATTERNS: dict[str, "re.Pattern[str]"] = {
    name: re.compile(pattern)
    for name, pattern in {
        "entity": rf"ENT-{D}{{6}}",
        "bank_account": rf"BANKACC-ENT-{D}{{6}}-{D}{{3}}",
        "ledger_entry": rf"LED-{D}{{4}}-{D}{{8}}",
        "cash_flow": rf"CF-{D}{{4}}-{D}{{6}}",
        "forecast_run": rf"FCAST-{D}{{4}}-{D}{{2}}-{D}{{2}}-{D}{{3}}",
        "receivable": rf"RCV-{D}{{4}}-{D}{{6}}",
        "payable": rf"PAY-{D}{{4}}-{D}{{6}}",
        "currency_rate": rf"FXR-[A-Z]{{3}}-{D}{{8}}",
        "scenario": rf"SCN-{D}{{3}}",
        "comparison": rf"VAR-{D}{{4}}-{D}{{6}}",
    }.items()
}


# ----------------------------------------------------------------------------
# validation helpers
# ----------------------------------------------------------------------------
def _counter(n: int, width: int, label: str) -> str:
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"{label} counter must be an int, got {n!r}")
    if not 1 <= n <= 10**width - 1:
        raise ValueError(f"{label} counter must be in 1..{10**width - 1}, got {n}")
    return str(n).zfill(width)


def _year(year: int) -> str:
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
        raise ValueError(f"year must be a 4-digit int, got {year!r}")
    return str(year)


_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _iso(d: DateLike) -> str:
    """Strict YYYY-MM-DD from a date or string; rejects impossible dates."""
    if isinstance(d, date):
        return d.isoformat()
    if isinstance(d, str) and _ISO_DATE.fullmatch(d):
        return date.fromisoformat(d).isoformat()  # raises ValueError for 2026-02-30
    raise ValueError(f"expected a date or 'YYYY-MM-DD', got {d!r}")


def _require(kind: str, value: str) -> str:
    if not isinstance(value, str) or not ID_PATTERNS[kind].fullmatch(value):
        raise ValueError(f"not a valid {kind} id: {value!r}")
    return value


# ----------------------------------------------------------------------------
# stateless formatters
# ----------------------------------------------------------------------------
def format_entity_id(n: int) -> str:
    return f"ENT-{_counter(n, ENTITY_WIDTH, 'entity')}"


def format_bank_account_id(entity_id: str, seq: int) -> str:
    _require("entity", entity_id)
    return f"BANKACC-{entity_id}-{_counter(seq, ACCOUNT_WIDTH, 'account')}"


def format_ledger_entry_id(year: int, n: int) -> str:
    return f"LED-{_year(year)}-{_counter(n, LEDGER_WIDTH, 'ledger')}"


def format_cash_flow_id(year: int, n: int) -> str:
    return f"CF-{_year(year)}-{_counter(n, CASH_FLOW_WIDTH, 'cash flow')}"


def format_forecast_run_id(cutoff: DateLike, seq: int) -> str:
    return f"FCAST-{_iso(cutoff)}-{_counter(seq, RUN_WIDTH, 'run')}"


def format_receivable_id(year: int, n: int) -> str:
    return f"RCV-{_year(year)}-{_counter(n, RECEIVABLE_WIDTH, 'receivable')}"


def format_payable_id(year: int, n: int) -> str:
    return f"PAY-{_year(year)}-{_counter(n, PAYABLE_WIDTH, 'payable')}"


def format_currency_rate_id(currency: str, rate_date: DateLike) -> str:
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"currency must be 3 uppercase letters, got {currency!r}")
    return f"FXR-{currency}-{_iso(rate_date).replace('-', '')}"


def format_scenario_id(n: int) -> str:
    return f"SCN-{_counter(n, SCENARIO_WIDTH, 'scenario')}"


def format_comparison_id(year: int, n: int) -> str:
    return f"VAR-{_year(year)}-{_counter(n, COMPARISON_WIDTH, 'comparison')}"


# ----------------------------------------------------------------------------
# parsing / classification
# ----------------------------------------------------------------------------
def id_kind(value: str) -> str | None:
    """Which ID format ``value`` matches, or None."""
    if not isinstance(value, str):
        return None
    for kind, pattern in ID_PATTERNS.items():
        if pattern.fullmatch(value):
            return kind
    return None


def split_bank_account_id(bank_account_id: str) -> tuple[str, int]:
    """'BANKACC-ENT-000001-002' -> ('ENT-000001', 2). Used to check an account ID
    against its entity_id and account_seq columns."""
    _require("bank_account", bank_account_id)
    entity_id = bank_account_id[len("BANKACC-"):-4]
    return entity_id, int(bank_account_id[-3:])


def year_of(identifier: str) -> int:
    """Year embedded in LED-, CF-, RCV-, PAY-, VAR- IDs."""
    kind = id_kind(identifier)
    if kind not in {"ledger_entry", "cash_flow", "receivable", "payable", "comparison"}:
        raise ValueError(f"id has no embedded year: {identifier!r}")
    return int(identifier.split("-")[1])


def sequence_of(identifier: str) -> int:
    """Trailing counter of any ID that ends in one (not currency-rate IDs)."""
    kind = id_kind(identifier)
    if kind is None or kind == "currency_rate":
        raise ValueError(f"id has no trailing counter: {identifier!r}")
    return int(identifier.rsplit("-", 1)[1])


# ----------------------------------------------------------------------------
# stateful factory
# ----------------------------------------------------------------------------
class IdFactory:
    """Issues IDs in call order. Counters only increase; nothing is ever reissued.

    Ledger, cash-flow, receivable, payable and comparison counters are kept per
    year, forecast-run counters per cutoff date, account counters per entity.
    To get ledger IDs that follow posting order, sort the records first and then
    ask for IDs in that order.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple, int] = {}

    def _issue(self, key: tuple, fmt) -> str:
        """Format the next counter value and only then commit it, so a rejected
        value (overflow, bad year) never burns a counter."""
        n = self._counts.get(key, 0) + 1
        value = fmt(n)
        self._counts[key] = n
        return value

    def issued(self, *key: object) -> int:
        """How many IDs have been issued under a counter key (0 if none)."""
        return self._counts.get(key, 0)

    def entity(self) -> str:
        return self._issue(("entity",), format_entity_id)

    def bank_account(self, entity_id: str) -> str:
        _require("entity", entity_id)
        return self._issue(("bank_account", entity_id),
                           lambda n: format_bank_account_id(entity_id, n))

    def ledger_entry(self, year: int) -> str:
        return self._issue(("ledger_entry", year), lambda n: format_ledger_entry_id(year, n))

    def cash_flow(self, year: int) -> str:
        return self._issue(("cash_flow", year), lambda n: format_cash_flow_id(year, n))

    def forecast_run(self, cutoff: DateLike) -> str:
        iso = _iso(cutoff)
        return self._issue(("forecast_run", iso), lambda n: format_forecast_run_id(iso, n))

    def receivable(self, year: int) -> str:
        return self._issue(("receivable", year), lambda n: format_receivable_id(year, n))

    def payable(self, year: int) -> str:
        return self._issue(("payable", year), lambda n: format_payable_id(year, n))

    def scenario(self) -> str:
        return self._issue(("scenario",), format_scenario_id)

    def comparison(self, year: int) -> str:
        return self._issue(("comparison", year), lambda n: format_comparison_id(year, n))
