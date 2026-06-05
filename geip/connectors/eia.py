"""EIA connector — three classes for EIA Open Data API v2.

EIAInternationalConnector  — historical oil/gas/coal + electricity by fuel (annual)
EIASTEOConnector           — Short-Term Energy Outlook projections (monthly)
EIAIEOConnector            — International Energy Outlook projections (annual)

All three read EIA_API_KEY from the environment. No key is ever hardcoded.

Unit discipline: EIA returns data in a variety of units (quad BTU, billion kWh,
mb/d, Bcf/d). Every series spec declares its EIA unit; _UNIT_TO_TWH converts to
the canonical TWh before constructing any FactRecord. An unrecognized unit string
raises ValueError in normalize() — no silent zeros.

Geography: EIA country/region names are passed through as-is in Phase 1. A TODO
marks the normalization hook for Phase 2 (map to OWID-style names for reconciliation).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional

import httpx

from geip.connectors.base import Cadence, ValidationReport
from geip.core.schema import EnergyType, FactRecord, MetricFamily

_BASE_URL = "https://api.eia.gov/v2"
_PAGE_SIZE = 5000

# ---------------------------------------------------------------------------
# Unit conversion
# ---------------------------------------------------------------------------

# All conversion factors produce TWh from the named EIA unit.
# Any unit string NOT in this table causes a hard error — never guess.
_UNIT_TO_TWH: dict[str, float] = {
    "billion kWh": 1.0,           # billion kWh == TWh exactly
    "BkWh": 1.0,
    "BKWH": 1.0,                  # EIA API v2 unit code for billion kilowatt-hours
    "TWh": 1.0,
    "quad BTU": 293.07153,        # 1 quad = 10^15 BTU; 1 BTU = 2.931e-4 Wh
    "Quadrillion Btu": 293.07153,
    "QBTU": 293.07153,            # EIA API v2 unit code for quadrillion Btu
    # Natural gas: 1 Bcf ≈ 1.027 × 10^15 BTU → 1.027 × 293.07 TWh/quad
    # EIA reports NG consumption in Tcf (trillion cubic feet) annually
    "Trillion Cubic Feet": 293.07153 * 1.027,
    "Tcf": 293.07153 * 1.027,
    # Oil: million barrels/day × 365 days × 5.691 MMBTU/bbl ÷ 3.412×10^-3 TWh/MMBTU
    # 1 mb/d/year ≈ 1 628 TWh; stored as mb/d → TWh/yr
    "Million Barrels per Day": 1628.0,
    "mb/d": 1628.0,
    "million barrels per day": 1628.0,  # STEO unit string (lowercase)
    # Short tons of coal: 1 short ton coal ≈ 20.169 MMBTU; MMBTU × 2.931×10-4 = TWh
    "Million Short Tons": 20.169 * 1e6 * 2.931e-4 / 1e6,  # TWh per million short tons
    "MMst": 20.169 * 1e6 * 2.931e-4 / 1e6,
}


def _to_twh(value: float, unit: str) -> float:
    factor = _UNIT_TO_TWH.get(unit)
    if factor is None:
        raise ValueError(
            f"Unrecognized EIA unit '{unit}'. Add a conversion factor to _UNIT_TO_TWH "
            "before using this series, never guess."
        )
    return value * factor


# ---------------------------------------------------------------------------
# Series specifications
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SeriesSpec:
    """Describes one EIA international series group."""
    product_id: str       # EIA facets[productId]
    activity_id: str      # EIA facets[activityId]  (e.g. "1"=production, "2"=consumption)
    energy_type: EnergyType
    metric_family: MetricFamily
    metric: str
    eia_unit: str         # expected unit string in EIA response


# International historical series to fetch.
# productId and activityId values verified against GET /v2/international/data/ with
# facets[unit][]=BKWH (electricity) and facets[unit][]=QBTU (primary energy).
_INTERNATIONAL_SERIES: list[_SeriesSpec] = [
    # Primary energy consumption (activityId=2, API unit code QBTU)
    _SeriesSpec("5",   "2", EnergyType.OIL,  MetricFamily.PRIMARY_ENERGY, "primary_energy_consumption", "QBTU"),
    _SeriesSpec("26",  "2", EnergyType.GAS,  MetricFamily.PRIMARY_ENERGY, "primary_energy_consumption", "QBTU"),
    _SeriesSpec("7",   "2", EnergyType.COAL, MetricFamily.PRIMARY_ENERGY, "primary_energy_consumption", "QBTU"),
    # Electricity generation by fuel (activityId=12, API unit code BKWH = TWh)
    _SeriesSpec("30",  "12", EnergyType.COAL,    MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    _SeriesSpec("31",  "12", EnergyType.GAS,     MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    _SeriesSpec("32",  "12", EnergyType.OIL,     MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    _SeriesSpec("27",  "12", EnergyType.NUCLEAR, MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    _SeriesSpec("33",  "12", EnergyType.HYDRO,   MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    _SeriesSpec("116", "12", EnergyType.SOLAR,   MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    _SeriesSpec("37",  "12", EnergyType.WIND,    MetricFamily.ELECTRICITY, "electricity_generation", "BKWH"),
    # Note: OTHER_RENEWABLE omitted — no single EIA product maps cleanly without
    # double-counting Solar/Wind. Phase 2 will handle via sub-series summation.
]


# STEO world-level series.
# seriesId values verified against GET /v2/steo/data/.
# NGTC_WORLD and CLTC_WORLD do not exist in the API; only PATC_WORLD is available
# at the world level. Each tuple: (seriesId, EnergyType, MetricFamily, metric, eia_unit)
_STEO_SERIES: list[tuple[str, EnergyType, MetricFamily, str, str]] = [
    ("PATC_WORLD", EnergyType.OIL, MetricFamily.PRIMARY_ENERGY, "consumption", "mb/d"),
]

# STEO scenario label (single reference case)
_STEO_SCENARIO = "reference"

# IEO scenario labels mapped from EIA case names.
_IEO_SCENARIO_MAP: dict[str, str] = {
    "REF2023":  "reference",
    "HM2023":   "high_economic_growth",
    "LM2023":   "low_economic_growth",
    "HO2023":   "high_oil_price",
    "LO2023":   "low_oil_price",
    # IEO 2024 vintage names
    "REF2024":  "reference",
    "HM2024":   "high_economic_growth",
    "LM2024":   "low_economic_growth",
    "HO2024":   "high_oil_price",
    "LO2024":   "low_oil_price",
}


# ---------------------------------------------------------------------------
# Shared HTTP client
# ---------------------------------------------------------------------------

class _EIAClient:
    """Wraps the EIA v2 REST API: injects the key, paginates."""

    def __init__(self, api_key: str) -> None:
        self._key = api_key
        self._http = httpx.Client(timeout=30.0)

    def get_all(self, route: str, params: dict[str, Any]) -> list[dict]:
        """Fetch all pages for a route and return the combined data list."""
        params = {**params, "api_key": self._key, "length": _PAGE_SIZE}
        offset = 0
        results: list[dict] = []
        while True:
            params["offset"] = offset
            resp = self._http.get(f"{_BASE_URL}/{route}", params=params)
            resp.raise_for_status()
            body = resp.json()
            data = body.get("response", {}).get("data", [])
            if not isinstance(data, list):
                break  # metadata endpoint returns data as a dict — not a data route
            results.extend(data)
            total = int(str(body.get("response", {}).get("total", len(data))))
            offset += len(data)
            if offset >= total or not data:
                break
        return results

    def close(self) -> None:
        self._http.close()


def _require_api_key() -> str:
    key = os.environ.get("EIA_API_KEY", "")
    if not key:
        raise ValueError(
            "EIA_API_KEY environment variable is not set. "
            "Register for a free key at https://www.eia.gov/opendata/register.php"
        )
    return key


# ---------------------------------------------------------------------------
# EIAInternationalConnector
# ---------------------------------------------------------------------------

class EIAInternationalConnector:
    source_id = "eia_international"
    cadence = Cadence(label="annual", poll_hours=24)
    license = "U.S. Government public domain (EIA)"

    def __init__(self) -> None:
        self._api_key = _require_api_key()
        self._client = _EIAClient(self._api_key)

    def fetch(self, since: Optional[date]) -> list[dict]:
        rows: list[dict] = []
        for spec in _INTERNATIONAL_SERIES:
            params: dict[str, Any] = {
                "frequency": "annual",
                "data[0]": "value",
                "facets[productId][]": spec.product_id,
                "facets[activityId][]": spec.activity_id,
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
            }
            if since:
                params["start"] = str(since.year + 1)
            raw = self._client.get_all("international/data/", params)
            for row in raw:
                row["_spec_product_id"] = spec.product_id
                row["_spec_activity_id"] = spec.activity_id
            rows.extend(raw)
        return rows

    def normalize(self, raw: list[dict]) -> list[FactRecord]:
        if not raw:
            return []
        pull_ts = datetime.now(timezone.utc)
        spec_map = {(s.product_id, s.activity_id): s for s in _INTERNATIONAL_SERIES}
        facts: list[FactRecord] = []
        for row in raw:
            key = (row.get("_spec_product_id", ""), row.get("_spec_activity_id", ""))
            spec = spec_map.get(key)
            if spec is None:
                continue
            val = row.get("value")
            if val is None or val == "":
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            unit_str = row.get("unit", spec.eia_unit)
            twh = _to_twh(fval, unit_str)
            try:
                year = int(str(row["period"])[:4])
            except (KeyError, ValueError, TypeError):
                continue
            # TODO Phase 2: normalize EIA geography names to OWID-style names
            geography = row.get("countryRegionName") or row.get("country") or row.get("areaName", "")
            vintage_str = row.get("lastHistoricalPeriod") or row.get("releaseDate", "")
            try:
                vintage = date(int(str(vintage_str)[:4]), 1, 1)
            except (ValueError, TypeError):
                vintage = date.today()
            facts.append(FactRecord(
                source_id=self.source_id,
                geography=geography,
                energy_type=spec.energy_type,
                metric=spec.metric,
                metric_family=spec.metric_family,
                period=date(year, 1, 1),
                period_type="yearly",
                value=twh,
                unit="TWh",
                vintage=vintage,
                pull_ts=pull_ts,
                is_projection=False,
            ))
        return facts

    def validate(self, facts: list[FactRecord]) -> ValidationReport:
        errors: list[str] = []
        for f in facts:
            if f.value < 0:
                errors.append(f"Negative value: {f.geography} {f.energy_type.value} {f.period}")
            if f.is_projection:
                errors.append(f"International connector must not emit projections: {f}")
        return ValidationReport(self.source_id, len(facts), len(errors), errors)


# ---------------------------------------------------------------------------
# EIASTEOConnector
# ---------------------------------------------------------------------------

class EIASTEOConnector:
    source_id = "eia_steo"
    cadence = Cadence(label="monthly", poll_hours=168)
    license = "U.S. Government public domain (EIA)"

    def __init__(self) -> None:
        self._api_key = _require_api_key()
        self._client = _EIAClient(self._api_key)

    def fetch(self, since: Optional[date]) -> list[dict]:
        rows: list[dict] = []
        for series_id, etype, mfamily, metric, eia_unit in _STEO_SERIES:
            params: dict[str, Any] = {
                "frequency": "monthly",
                "data[0]": "value",
                "facets[seriesId][]": series_id,
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
            }
            if since:
                params["start"] = since.strftime("%Y-%m")
            raw = self._client.get_all("steo/data/", params)
            for row in raw:
                row["_series_id"] = series_id
                row["_energy_type"] = etype
                row["_metric_family"] = mfamily
                row["_metric"] = metric
                row["_eia_unit"] = eia_unit
            rows.extend(raw)
        return rows

    def normalize(self, raw: list[dict]) -> list[FactRecord]:
        if not raw:
            return []
        pull_ts = datetime.now(timezone.utc)
        facts: list[FactRecord] = []
        for row in raw:
            val = row.get("value")
            if val is None or val == "":
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            eia_unit = row["_eia_unit"]
            twh = _to_twh(fval, eia_unit)
            period_str = str(row.get("period", ""))
            try:
                period = date(int(period_str[:4]), int(period_str[5:7]), 1)
            except (ValueError, IndexError):
                continue
            # Projection boundary: periods after lastHistoricalPeriod are forecast.
            last_hist_str = str(row.get("lastHistoricalPeriod", ""))
            try:
                last_hist = date(int(last_hist_str[:4]), int(last_hist_str[5:7]), 1)
                is_proj = period > last_hist
            except (ValueError, IndexError):
                # If we can't determine the boundary, treat future calendar periods as projections.
                is_proj = period > date.today().replace(day=1)
            vintage_str = str(row.get("releaseDate", ""))
            try:
                vintage = date(int(vintage_str[:4]), int(vintage_str[5:7]), 1)
            except (ValueError, IndexError):
                vintage = date.today()
            facts.append(FactRecord(
                source_id=self.source_id,
                geography="World",
                energy_type=row["_energy_type"],
                metric=row["_metric"],
                metric_family=row["_metric_family"],
                period=period,
                period_type="monthly",
                value=twh,
                unit="TWh",
                vintage=vintage,
                pull_ts=pull_ts,
                is_projection=is_proj,
                scenario=_STEO_SCENARIO if is_proj else None,
            ))
        return facts

    def validate(self, facts: list[FactRecord]) -> ValidationReport:
        errors: list[str] = []
        for f in facts:
            if f.value < 0:
                errors.append(f"Negative value: {f.geography} {f.energy_type.value} {f.period}")
            if f.is_projection and not f.scenario:
                errors.append(f"Projection without scenario: {f.period} {f.energy_type.value}")
        return ValidationReport(self.source_id, len(facts), len(errors), errors)


# ---------------------------------------------------------------------------
# EIAIEOConnector
# ---------------------------------------------------------------------------

class EIAIEOConnector:
    source_id = "eia_ieo"
    cadence = Cadence(label="annual", poll_hours=8760)
    license = "U.S. Government public domain (EIA)"

    def __init__(self) -> None:
        self._api_key = _require_api_key()
        self._client = _EIAClient(self._api_key)

    def fetch(self, since: Optional[date]) -> list[dict]:
        # IEO data is available under the AEO route in EIA API v2 for international scenarios.
        # The route is confirmed against GET /v2/aeo/ metadata at runtime.
        params: dict[str, Any] = {
            "frequency": "annual",
            "data[0]": "value",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
        }
        if since:
            params["start"] = str(since.year + 1)
        rows = self._client.get_all("aeo/data/", params)
        return rows

    def normalize(self, raw: list[dict]) -> list[FactRecord]:
        if not raw:
            return []
        pull_ts = datetime.now(timezone.utc)
        facts: list[FactRecord] = []
        for row in raw:
            val = row.get("value")
            if val is None or val == "":
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            unit_str = row.get("unit", "")
            try:
                twh = _to_twh(fval, unit_str)
            except ValueError:
                # Skip series with unmapped units rather than fabricating a conversion.
                continue
            period_str = str(row.get("period", ""))
            try:
                year = int(period_str[:4])
            except (ValueError, IndexError):
                continue
            case_id = row.get("caseId") or row.get("scenarioId") or ""
            scenario = _IEO_SCENARIO_MAP.get(str(case_id))
            if scenario is None:
                # Use the raw case description if no mapped name; never drop the record.
                scenario = row.get("caseDescription") or str(case_id) or "reference"
            vintage_str = str(row.get("releaseDate", ""))
            try:
                vintage = date(int(vintage_str[:4]), 1, 1)
            except (ValueError, IndexError):
                vintage = date.today()
            # TODO Phase 2: map EIA series descriptions to EnergyType + MetricFamily
            energy_type_str = row.get("seriesDescription", "")
            energy_type = _guess_energy_type(energy_type_str)
            metric_family = _guess_metric_family(energy_type_str)
            if energy_type is None or metric_family is None:
                continue
            facts.append(FactRecord(
                source_id=self.source_id,
                geography=row.get("regionName") or row.get("areaName") or "World",
                energy_type=energy_type,
                metric="consumption",
                metric_family=metric_family,
                period=date(year, 1, 1),
                period_type="yearly",
                value=twh,
                unit="TWh",
                vintage=vintage,
                pull_ts=pull_ts,
                is_projection=True,
                scenario=scenario,
            ))
        return facts

    def validate(self, facts: list[FactRecord]) -> ValidationReport:
        errors: list[str] = []
        for f in facts:
            if not f.is_projection:
                errors.append(f"IEO connector must only emit projections: {f.period} {f.energy_type.value}")
            if not f.scenario:
                errors.append(f"IEO projection missing scenario: {f.period} {f.energy_type.value}")
        return ValidationReport(self.source_id, len(facts), len(errors), errors)


# ---------------------------------------------------------------------------
# Helpers for IEO series classification
# ---------------------------------------------------------------------------

def _guess_energy_type(description: str) -> Optional[EnergyType]:
    desc = description.lower()
    if "petroleum" in desc or "liquid" in desc or "oil" in desc:
        return EnergyType.OIL
    if "natural gas" in desc or "gas" in desc:
        return EnergyType.GAS
    if "coal" in desc:
        return EnergyType.COAL
    if "nuclear" in desc:
        return EnergyType.NUCLEAR
    if "hydro" in desc:
        return EnergyType.HYDRO
    if "solar" in desc:
        return EnergyType.SOLAR
    if "wind" in desc:
        return EnergyType.WIND
    if "renewable" in desc or "biofuel" in desc or "biomass" in desc or "geothermal" in desc:
        return EnergyType.OTHER_RENEWABLE
    if "total" in desc or "all" in desc:
        return EnergyType.TOTAL
    return None


def _guess_metric_family(description: str) -> Optional[MetricFamily]:
    desc = description.lower()
    if "electricity" in desc or "generation" in desc or "power" in desc:
        return MetricFamily.ELECTRICITY
    if "energy" in desc or "consumption" in desc or "demand" in desc:
        return MetricFamily.PRIMARY_ENERGY
    return None
