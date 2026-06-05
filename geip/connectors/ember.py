"""Ember connector — power-sector electricity generation, capacity, and demand
for 215 countries.

Uses the Ember Electricity Data API (https://api.ember-energy.org/v1/).
Set EMBER_API_KEY environment variable. Free registration:
https://ember-energy.org/data/api/

API migration note (api.ember-climate.org → api.ember-energy.org):
  - Base URL changed.
  - Endpoints now carry temporal resolution: electricity-generation/yearly,
    installed-capacity/monthly (not just electricity-generation, installed-capacity).
  - Response envelope changed: {"stats": {"number_of_records": N}, "data": [...]}
    (was {"data": [...], "meta": {"total": N}}). No offset/limit pagination —
    the API returns all matching records in one call.
  - Per-row field names changed: country_or_region→entity, year→date,
    subcategory→series. Value and unit are now a single typed field
    (generation_twh, capacity_gw) instead of value+unit strings.
  - Capacity series split: "Offshore wind" and "Onshore wind" are separate
    non-aggregate series; "Wind" is their aggregate. We map the aggregate
    "Wind" and "Solar" to avoid summing sub-series twice.

Bioenergy handling (unchanged from prior version):
  Ember reports "Bioenergy" and "Other renewables" as separate series.
  OWID (the canonical spine) folds bioenergy into other_renewable_electricity.
  normalize() sums Bioenergy + Other renewables into a single OTHER_RENEWABLE
  FactRecord per (geography, date). Orphan bioenergy (no paired Other renewables
  row) is still emitted as OTHER_RENEWABLE. test_bioenergy_folded_into_other_renewable
  and test_orphan_bioenergy_still_emitted guard both paths.

CO2 intensity (gCO2/kWh) is deferred to Phase 2 — needs a rate MetricFamily.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from geip.connectors.base import Cadence, ValidationReport
from geip.core.schema import EnergyType, FactRecord, MetricFamily

_BASE_URL = "https://api.ember-energy.org/v1"

# Ember series label for bioenergy — handled specially in normalize().
_BIOENERGY_LABEL = "Bioenergy"


# ---------------------------------------------------------------------------
# Endpoint specifications
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _EndpointSpec:
    """Describes one Ember API endpoint and how to map its records to FactRecords."""
    path: str                            # e.g. "electricity-generation/yearly"
    metric: str                          # GEIP metric name
    metric_family: MetricFamily
    canonical_unit: str                  # must match CANONICAL_UNIT[metric_family]
    value_field: str                     # field in API response carrying the value
    period_type: str                     # "yearly" | "monthly"
    series_map: dict[str, EnergyType]    # API series name → EnergyType (absent = skip)
    exclude_aggregate_series: bool       # whether to send is_aggregate_series=false


_ENDPOINT_SPECS: list[_EndpointSpec] = [
    _EndpointSpec(
        path="electricity-generation/yearly",
        metric="electricity_generation",
        metric_family=MetricFamily.ELECTRICITY,
        canonical_unit="TWh",
        value_field="generation_twh",
        period_type="yearly",
        series_map={
            "Coal":             EnergyType.COAL,
            "Gas":              EnergyType.GAS,
            "Other fossil":     EnergyType.OIL,
            "Nuclear":          EnergyType.NUCLEAR,
            "Hydro":            EnergyType.HYDRO,
            "Solar":            EnergyType.SOLAR,
            "Wind":             EnergyType.WIND,
            "Other renewables": EnergyType.OTHER_RENEWABLE,
            # "Bioenergy" handled via _BIOENERGY_LABEL, folded into OTHER_RENEWABLE
        },
        exclude_aggregate_series=True,
    ),
    _EndpointSpec(
        path="installed-capacity/monthly",
        metric="installed_capacity",
        metric_family=MetricFamily.CAPACITY,
        canonical_unit="GW",
        value_field="capacity_gw",
        period_type="monthly",
        series_map={
            # "Wind" is the aggregate of Offshore wind + Onshore wind.
            # We map the aggregate to avoid double-counting sub-series.
            "Wind":  EnergyType.WIND,
            "Solar": EnergyType.SOLAR,
        },
        # Capacity "Wind" aggregate has is_aggregate_series=True; don't filter it out.
        exclude_aggregate_series=False,
    ),
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _require_api_key() -> str:
    key = os.environ.get("EMBER_API_KEY", "")
    if not key:
        raise ValueError(
            "EMBER_API_KEY environment variable is not set. "
            "Register for free at https://ember-energy.org/data/api/"
        )
    return key


# ---------------------------------------------------------------------------
# Period parsing
# ---------------------------------------------------------------------------

def _parse_period(date_str: Any, period_type: str) -> Optional[date]:
    """Parse an Ember date string into a date.

    Yearly endpoints return "YYYY"; monthly endpoints return "YYYY-MM-DD"
    (confirmed from live API — the spec says YYYY-MM but the actual response
    uses full ISO dates). We handle both formats.
    """
    s = str(date_str)
    try:
        year = int(s[:4])
        if period_type == "monthly" and len(s) >= 7:
            month = int(s[5:7])
        else:
            month = 1
        return date(year, month, 1)
    except (ValueError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Bioenergy merge helpers
# ---------------------------------------------------------------------------

# Key: (geography, date_str) — uniquely identifies one bioenergy accumulation target.
# Value: (accumulated_value, spec) — spec is needed to build the orphan FactRecord.
_BioKey = tuple[str, str]


def _build_bioenergy_map(raw: list[dict]) -> dict[_BioKey, tuple[float, _EndpointSpec]]:
    """Pass 1: accumulate bioenergy values per (geography, date_str)."""
    acc: dict[_BioKey, tuple[float, _EndpointSpec]] = {}
    for row in raw:
        if row.get("series") != _BIOENERGY_LABEL:
            continue
        spec: Optional[_EndpointSpec] = row.get("_spec")
        if spec is None:
            continue
        geo = row.get("entity", "")
        date_str = str(row.get("date", ""))
        raw_val = row.get(spec.value_field)
        if raw_val is None:
            continue
        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            continue
        key: _BioKey = (geo, date_str)
        existing_val, _ = acc.get(key, (0.0, spec))
        acc[key] = (existing_val + val, spec)
    return acc


def _emit_orphan_bioenergy(
    bioenergy: dict[_BioKey, tuple[float, _EndpointSpec]],
    consumed: set[_BioKey],
    pull_ts: datetime,
    source_id: str,
) -> list[FactRecord]:
    """Emit OTHER_RENEWABLE records for bioenergy keys with no matching
    'Other renewables' row (data-sparse geographies)."""
    facts: list[FactRecord] = []
    for (geo, date_str), (val, spec) in bioenergy.items():
        if (geo, date_str) in consumed:
            continue
        period = _parse_period(date_str, spec.period_type)
        if period is None:
            continue
        facts.append(FactRecord(
            source_id=source_id,
            geography=geo,
            energy_type=EnergyType.OTHER_RENEWABLE,
            metric=spec.metric,
            metric_family=spec.metric_family,
            period=period,
            period_type=spec.period_type,
            value=val,
            unit=spec.canonical_unit,
            vintage=date(period.year, 12, 31),
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
        """Fetch all matching records from each endpoint.

        The new API returns all matching records in a single response (no
        offset/limit pagination). Records are tagged with _spec so normalize()
        knows which endpoint they came from without inspecting field names.
        """
        rows: list[dict] = []
        for spec in _ENDPOINT_SPECS:
            params: dict[str, Any] = {"api_key": self._api_key}
            if spec.exclude_aggregate_series:
                params["is_aggregate_series"] = "false"
            if since:
                if spec.period_type == "monthly":
                    params["start_date"] = since.strftime("%Y-%m")
                else:
                    params["start_date"] = str(since.year + 1)
            resp = self._http.get(f"{_BASE_URL}/{spec.path}", params=params)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("data", [])
            for row in data:
                row["_spec"] = spec
            rows.extend(data)
        return rows

    def normalize(self, raw: list[dict]) -> list[FactRecord]:
        if not raw:
            return []
        pull_ts = datetime.now(timezone.utc)

        # Pass 1: accumulate bioenergy per (geography, date_str) for folding.
        bioenergy = _build_bioenergy_map(raw)
        consumed_bio_keys: set[_BioKey] = set()

        facts: list[FactRecord] = []
        for row in raw:
            spec: Optional[_EndpointSpec] = row.get("_spec")
            if spec is None:
                continue

            series = row.get("series", "")
            if series == _BIOENERGY_LABEL:
                continue  # handled via bioenergy map

            etype = spec.series_map.get(series)
            if etype is None:
                continue  # aggregate or unknown series — skip

            raw_val = row.get(spec.value_field)
            if raw_val is None:
                continue
            try:
                val = float(raw_val)
            except (TypeError, ValueError):
                continue

            date_str = str(row.get("date", ""))
            period = _parse_period(date_str, spec.period_type)
            if period is None:
                continue

            # TODO Phase 2: normalise Ember geography names → OWID-style names
            geo = row.get("entity", "")

            # Fold bioenergy into OTHER_RENEWABLE for this (geography, date) pair.
            if etype is EnergyType.OTHER_RENEWABLE:
                bio_key: _BioKey = (geo, date_str)
                bio_val, _ = bioenergy.get(bio_key, (0.0, spec))
                val += bio_val
                consumed_bio_keys.add(bio_key)

            vintage = date(period.year, 12, 31)

            facts.append(FactRecord(
                source_id=self.source_id,
                geography=geo,
                energy_type=etype,
                metric=spec.metric,
                metric_family=spec.metric_family,
                period=period,
                period_type=spec.period_type,
                value=val,
                unit=spec.canonical_unit,
                vintage=vintage,
                pull_ts=pull_ts,
                is_projection=False,
            ))

        # Pass 3: emit OTHER_RENEWABLE for bioenergy with no paired Other renewables row.
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
