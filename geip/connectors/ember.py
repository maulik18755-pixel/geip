"""Ember connector — power-sector electricity generation, capacity, and demand
for 215 countries.

Uses the Ember Electricity Data API (https://api.ember-climate.org/v1/).
Set EMBER_API_KEY environment variable. Free registration:
https://ember-climate.org/data-catalogue/

Bioenergy handling (critical):
  Ember reports "Bioenergy" and "Other renewables" as separate subcategories.
  OWID (the canonical spine) folds bioenergy into other_renewable_electricity.
  normalize() sums Ember Bioenergy + Other renewables into a single
  OTHER_RENEWABLE FactRecord per (geography, year, variable), preserving
  reconcilability with the spine. test_bioenergy_folded_into_other_renewable
  guards this invariant.

  Edge case: if Ember emits Bioenergy for a geography/year with NO corresponding
  "Other renewables" row, bioenergy is still emitted as OTHER_RENEWABLE (not
  dropped). See _merge_other_renewables() for the implementation.

CO2 intensity (gCO2/kWh) is deferred to Phase 2 — it requires a separate
MetricFamily (rate, not total MtCO2). A TODO marks the hook point.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from geip.connectors.base import Cadence, ValidationReport
from geip.core.schema import EnergyType, FactRecord, MetricFamily

_BASE_URL = "https://api.ember-climate.org/v1"
_PAGE_SIZE = 10000

# Ember subcategory label for bioenergy — handled specially.
_BIOENERGY_LABEL = "Bioenergy"

# Ember subcategory → GEIP EnergyType.
# Aggregate labels (Fossil, Renewables, Total generation, Low-carbon, Clean)
# are intentionally absent — including them would double-count.
_SUBCATEGORY_MAP: dict[str, EnergyType] = {
    "Coal": EnergyType.COAL,
    "Gas": EnergyType.GAS,
    "Other fossil": EnergyType.OIL,
    "Nuclear": EnergyType.NUCLEAR,
    "Hydro": EnergyType.HYDRO,
    "Solar": EnergyType.SOLAR,
    "Wind": EnergyType.WIND,
    "Other renewables": EnergyType.OTHER_RENEWABLE,
    # "Bioenergy" handled via _BIOENERGY_LABEL, folded into OTHER_RENEWABLE
}

# Ember variable name → (GEIP metric, MetricFamily, canonical unit).
# The canonical unit is enforced by FactRecord; rows with a different unit raise.
_VARIABLE_MAP: dict[str, tuple[str, MetricFamily, str]] = {
    "Generation": ("electricity_generation", MetricFamily.ELECTRICITY, "TWh"),
    "Installed capacity": ("installed_capacity", MetricFamily.CAPACITY, "GW"),
    "Demand": ("electricity_demand", MetricFamily.ELECTRICITY, "TWh"),
    # TODO Phase 2: "Emissions intensity" (gCO2/kWh) needs a rate MetricFamily
}

# Endpoints to fetch per connector poll.
_ENDPOINTS = ["electricity-generation", "installed-capacity"]


def _require_api_key() -> str:
    key = os.environ.get("EMBER_API_KEY", "")
    if not key:
        raise ValueError(
            "EMBER_API_KEY environment variable is not set. "
            "Register for free at https://ember-climate.org/data-catalogue/"
        )
    return key


def _parse_year(val: Any) -> Optional[int]:
    try:
        return int(str(val)[:4])
    except (TypeError, ValueError):
        return None


def _parse_vintage(row: dict, data_year: int) -> date:
    """Return vintage date from row metadata, falling back to year-end of data year."""
    raw = row.get("published_date") or row.get("updated_at") or ""
    yr = _parse_year(raw)
    return date(yr, 1, 1) if yr else date(data_year, 12, 31)


# ---------------------------------------------------------------------------
# Bioenergy merge
# ---------------------------------------------------------------------------

# Key type for the bioenergy accumulator: (geography, data_year, variable)
_BioKey = tuple[str, int, str]


def _build_bioenergy_map(raw: list[dict]) -> dict[_BioKey, float]:
    """First pass: accumulate bioenergy TWh per (geography, year, variable)."""
    acc: dict[_BioKey, float] = {}
    for row in raw:
        if row.get("subcategory") != _BIOENERGY_LABEL:
            continue
        geo = row.get("country_or_region", "")
        yr = _parse_year(row.get("year"))
        var = row.get("variable", "")
        if yr is None:
            continue
        try:
            val = float(row["value"])
        except (TypeError, ValueError):
            continue
        key: _BioKey = (geo, yr, var)
        acc[key] = acc.get(key, 0.0) + val
    return acc


def _emit_orphan_bioenergy(
    bioenergy: dict[_BioKey, float],
    consumed: set[_BioKey],
    pull_ts: datetime,
    source_id: str,
) -> list[FactRecord]:
    """Emit OTHER_RENEWABLE records for bioenergy keys with no matching
    'Other renewables' row (rare but possible for small/data-sparse geographies)."""
    facts: list[FactRecord] = []
    for (geo, yr, var_key), val in bioenergy.items():
        if (geo, yr, var_key) in consumed:
            continue
        var_info = _VARIABLE_MAP.get(var_key)
        if var_info is None:
            continue
        metric, mfamily, canonical_unit = var_info
        facts.append(FactRecord(
            source_id=source_id,
            geography=geo,
            energy_type=EnergyType.OTHER_RENEWABLE,
            metric=metric,
            metric_family=mfamily,
            period=date(yr, 1, 1),
            period_type="yearly",
            value=val,
            unit=canonical_unit,
            vintage=date(yr, 12, 31),
            pull_ts=pull_ts,
            is_projection=False,
        ))
    return facts


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------

class EmberConnector:
    source_id = "ember"
    cadence = Cadence(label="twice_monthly", poll_hours=360)
    license = "CC BY 4.0 (Ember)"

    def __init__(self) -> None:
        self._api_key = _require_api_key()
        self._http = httpx.Client(timeout=30.0)

    def fetch(self, since: Optional[date]) -> list[dict]:
        rows: list[dict] = []
        for endpoint in _ENDPOINTS:
            params: dict[str, Any] = {
                "api_key": self._api_key,
                "limit": _PAGE_SIZE,
            }
            if since:
                params["start_year"] = since.year + 1
            offset = 0
            while True:
                params["offset"] = offset
                resp = self._http.get(f"{_BASE_URL}/{endpoint}", params=params)
                resp.raise_for_status()
                body = resp.json()
                data = body.get("data", [])
                rows.extend(data)
                total = body.get("meta", {}).get("total", len(data))
                offset += len(data)
                if offset >= total or not data:
                    break
        return rows

    def normalize(self, raw: list[dict]) -> list[FactRecord]:
        if not raw:
            return []
        pull_ts = datetime.now(timezone.utc)

        # Pass 1: collect bioenergy values so they can be folded into OTHER_RENEWABLE.
        bioenergy = _build_bioenergy_map(raw)
        consumed_bio_keys: set[_BioKey] = set()

        facts: list[FactRecord] = []
        for row in raw:
            subcat = row.get("subcategory", "")
            if subcat == _BIOENERGY_LABEL:
                continue  # handled via bioenergy map

            etype = _SUBCATEGORY_MAP.get(subcat)
            if etype is None:
                continue  # aggregate row — skip to avoid double-counting

            var_key = row.get("variable", "")
            var_info = _VARIABLE_MAP.get(var_key)
            if var_info is None:
                continue  # unrecognised variable (e.g. CO2 intensity) — deferred

            metric, mfamily, canonical_unit = var_info

            val_raw = row.get("value")
            if val_raw is None or val_raw == "":
                continue
            try:
                val = float(val_raw)
            except (TypeError, ValueError):
                continue

            # Hard error on unexpected unit — never silently misinterpret.
            row_unit = row.get("unit", "")
            if row_unit and row_unit != canonical_unit:
                raise ValueError(
                    f"Ember returned unit '{row_unit}' for variable '{var_key}'; "
                    f"expected '{canonical_unit}'. Update _VARIABLE_MAP if Ember changed units."
                )

            yr = _parse_year(row.get("year"))
            if yr is None:
                continue

            # TODO Phase 2: normalize Ember geography names → OWID-style names
            geo = row.get("country_or_region", "")

            # Fold bioenergy into the OTHER_RENEWABLE record for this (geo, year, variable).
            if etype is EnergyType.OTHER_RENEWABLE:
                bio_key: _BioKey = (geo, yr, var_key)
                val += bioenergy.get(bio_key, 0.0)
                consumed_bio_keys.add(bio_key)

            facts.append(FactRecord(
                source_id=self.source_id,
                geography=geo,
                energy_type=etype,
                metric=metric,
                metric_family=mfamily,
                period=date(yr, 1, 1),
                period_type="yearly",
                value=val,
                unit=canonical_unit,
                vintage=_parse_vintage(row, yr),
                pull_ts=pull_ts,
                is_projection=False,
            ))

        # Pass 3: emit OTHER_RENEWABLE for bioenergy rows that had no paired
        # "Other renewables" row (data-sparse geographies).
        facts.extend(_emit_orphan_bioenergy(bioenergy, consumed_bio_keys, pull_ts, self.source_id))
        return facts

    def validate(self, facts: list[FactRecord]) -> ValidationReport:
        errors: list[str] = []
        for f in facts:
            if f.value < 0:
                errors.append(f"Negative value: {f.geography} {f.energy_type.value} {f.period}")
            if f.is_projection:
                errors.append(f"Ember must not emit projections: {f.geography} {f.period}")
        return ValidationReport(self.source_id, len(facts), len(errors), errors)

    def close(self) -> None:
        self._http.close()
