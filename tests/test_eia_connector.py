"""Unit tests for EIA connector normalize() — no network calls.

All HTTP calls are mocked. Tests verify:
- canonical unit (TWh) on every emitted FactRecord
- is_projection=False for historical international records
- is_projection=True + scenario for STEO future periods and all IEO records
- missing API key raises ValueError before any HTTP call
- unknown unit raises ValueError in normalize()
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from geip.connectors.base import SourceConnector
from geip.connectors.eia import (
    EIAIEOConnector,
    EIAInternationalConnector,
    EIASTEOConnector,
    _to_twh,
)
from geip.core.schema import MetricFamily

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PULL_TS = datetime(2024, 6, 1, 0, 0, 0)


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIA_API_KEY", "TEST_KEY")


# ---------------------------------------------------------------------------
# Fixtures: minimal EIA API v2 response payloads
# ---------------------------------------------------------------------------

# International: one row per configured spec — all 10 series.
# Unit codes match real EIA API v2 /international/data/ responses.
_INTL_RAW: list[dict[str, Any]] = [
    # Primary energy consumption (activityId=2, QBTU)
    {"period": "2022", "countryRegionName": "World", "value": "200",   "unit": "QBTU", "releaseDate": "2023-06-01", "_spec_product_id": "5",   "_spec_activity_id": "2"},
    {"period": "2022", "countryRegionName": "World", "value": "100",   "unit": "QBTU", "releaseDate": "2023-06-01", "_spec_product_id": "26",  "_spec_activity_id": "2"},
    {"period": "2022", "countryRegionName": "World", "value": "150",   "unit": "QBTU", "releaseDate": "2023-06-01", "_spec_product_id": "7",   "_spec_activity_id": "2"},
    # Electricity generation (activityId=12, BKWH)
    {"period": "2022", "countryRegionName": "World", "value": "10000", "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "30",  "_spec_activity_id": "12"},
    {"period": "2022", "countryRegionName": "World", "value": "6500",  "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "31",  "_spec_activity_id": "12"},
    {"period": "2022", "countryRegionName": "World", "value": "900",   "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "32",  "_spec_activity_id": "12"},
    {"period": "2022", "countryRegionName": "World", "value": "2700",  "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "27",  "_spec_activity_id": "12"},
    {"period": "2022", "countryRegionName": "World", "value": "4400",  "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "33",  "_spec_activity_id": "12"},
    {"period": "2022", "countryRegionName": "World", "value": "1800",  "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "116", "_spec_activity_id": "12"},
    {"period": "2022", "countryRegionName": "World", "value": "2200",  "unit": "BKWH", "releaseDate": "2023-06-01", "_spec_product_id": "37",  "_spec_activity_id": "12"},
]

# Verbatim row captured from GET /v2/international/data/ (productId=30, activityId=12,
# countryRegionId=WORL, start/end=2022) — used as regression anchor for the BKWH path.
_INTL_REAL_SAMPLE: list[dict[str, Any]] = [
    {
        "period": "2022",
        "productId": "30",
        "productName": "Coal",
        "activityId": "12",
        "activityName": "Generation",
        "countryRegionId": "WORL",
        "countryRegionName": "World",
        "countryRegionTypeId": "r",
        "countryRegionTypeName": "Region",
        "dataFlagId": None,
        "dataFlagDescription": None,
        "unitName": "billion kilowatthours",
        "value": "9866.94913482",
        "unit": "BKWH",
        "_spec_product_id": "30",
        "_spec_activity_id": "12",
    }
]

# STEO: past period + future period in same series
_STEO_RAW: list[dict[str, Any]] = [
    {
        "period": "2023-06",
        "value": "100",
        "_series_id": "PATC_WORLD",
        "_energy_type": __import__("geip.core.schema", fromlist=["EnergyType"]).EnergyType.OIL,
        "_metric_family": MetricFamily.PRIMARY_ENERGY,
        "_metric": "consumption",
        "_eia_unit": "mb/d",
        "lastHistoricalPeriod": "2024-01",
        "releaseDate": "2024-02-01",
    },
    {
        "period": "2025-06",
        "value": "102",
        "_series_id": "PATC_WORLD",
        "_energy_type": __import__("geip.core.schema", fromlist=["EnergyType"]).EnergyType.OIL,
        "_metric_family": MetricFamily.PRIMARY_ENERGY,
        "_metric": "consumption",
        "_eia_unit": "mb/d",
        "lastHistoricalPeriod": "2024-01",
        "releaseDate": "2024-02-01",
    },
]

# IEO: two rows matching the real /v2/ieo/2023/data/ response shape.
# tableId=20 "Net electricity generation by region and fuel", unit="bill kWh".
# One Reference row + one HighMacro row so scenario-mapping tests cover both paths.
_IEO_RAW: list[dict[str, Any]] = [
    {
        "period": "2035",
        "history": "PROJECTION",
        "scenario": "Reference",
        "scenarioDescription": "Reference case",
        "tableId": "20",
        "tableName": "Net electricity generation by region and fuel",
        "seriesId": "elec_gen_cl_bkwh",
        "seriesName": "Net generation : Coal",
        "regionId": "6-0",
        "regionName": "Total World",
        "value": "9000",
        "unit": "bill kWh",
    },
    {
        "period": "2035",
        "history": "PROJECTION",
        "scenario": "HighMacro",
        "scenarioDescription": "High economic growth",
        "tableId": "20",
        "tableName": "Net electricity generation by region and fuel",
        "seriesId": "elec_gen_lf_bkwh",
        "seriesName": "Net generation : Liquid fuels",
        "regionId": "6-0",
        "regionName": "Total World",
        "value": "700",
        "unit": "bill kWh",
    },
]


# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------

def test_missing_api_key_international(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="EIA_API_KEY"):
        EIAInternationalConnector()


def test_missing_api_key_steo(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="EIA_API_KEY"):
        EIASTEOConnector()


def test_missing_api_key_ieo(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(ValueError, match="EIA_API_KEY"):
        EIAIEOConnector()


# ---------------------------------------------------------------------------
# EIAInternationalConnector
# ---------------------------------------------------------------------------

@pytest.fixture
def intl(monkeypatch):
    _set_key(monkeypatch)
    return EIAInternationalConnector()


def test_international_units_are_twh(intl):
    facts = intl.normalize(_INTL_RAW)
    assert facts, "normalize() returned nothing"
    assert all(f.unit == "TWh" for f in facts), [f.unit for f in facts]


def test_international_no_projections(intl):
    facts = intl.normalize(_INTL_RAW)
    assert all(not f.is_projection for f in facts)


def test_international_metric_family_matches_series(intl):
    facts = intl.normalize(_INTL_RAW)
    families = {f.metric_family for f in facts}
    # We sent three PRIMARY_ENERGY rows and seven ELECTRICITY rows
    assert MetricFamily.PRIMARY_ENERGY in families
    assert MetricFamily.ELECTRICITY in families


def test_international_value_conversion(intl):
    facts = intl.normalize(_INTL_RAW)
    # 200 QBTU × 293.07153 TWh/quad = 58614.306 TWh
    oil_facts = [f for f in facts if f.metric_family == MetricFamily.PRIMARY_ENERGY]
    assert oil_facts
    assert abs(oil_facts[0].value - 200 * 293.07153) < 0.01


def test_international_electricity_value(intl):
    facts = intl.normalize(_INTL_RAW)
    elec_facts = [f for f in facts if f.metric_family == MetricFamily.ELECTRICITY]
    assert elec_facts
    assert abs(elec_facts[0].value - 10000.0) < 0.01  # BKWH == TWh, factor 1.0


def test_real_bkwh_response_shape(intl):
    """Regression: real API returns unit='BKWH', not 'billion kWh'. Both must parse."""
    facts = intl.normalize(_INTL_REAL_SAMPLE)
    assert len(facts) == 1
    f = facts[0]
    assert f.unit == "TWh"
    assert f.geography == "World"
    assert f.period == date(2022, 1, 1)
    # BKWH → TWh factor is 1.0; value must round-trip exactly
    assert abs(f.value - 9866.94913482) < 0.001


def test_international_skips_empty_values(intl):
    raw = [dict(_INTL_RAW[0], value="")]
    facts = intl.normalize(raw)
    assert facts == []


def test_international_validate_ok(intl):
    facts = intl.normalize(_INTL_RAW)
    report = intl.validate(facts)
    assert report.ok, report.errors


def test_normalize_skips_wrong_unit_variant(intl):
    """MT and other mass/volume units must be silently dropped, not crash via _to_twh."""
    mt_row = dict(_INTL_RAW[0], unit="MT", value="5000000")
    assert intl.normalize([mt_row]) == []


def test_validate_flags_missing_series(intl):
    """If one expected series produces no facts, validate() must flag it loudly."""
    no_wind = [r for r in _INTL_RAW if r["_spec_product_id"] != "37"]
    facts = intl.normalize(no_wind)
    report = intl.validate(facts)
    assert not report.ok
    assert any("37" in e for e in report.errors), report.errors


def test_protocol_conformance_international(intl):
    assert isinstance(intl, SourceConnector)


# ---------------------------------------------------------------------------
# EIASTEOConnector
# ---------------------------------------------------------------------------

@pytest.fixture
def steo(monkeypatch):
    _set_key(monkeypatch)
    return EIASTEOConnector()


def test_steo_units_are_twh(steo):
    facts = steo.normalize(_STEO_RAW)
    assert facts
    assert all(f.unit == "TWh" for f in facts)


def test_steo_past_record_is_not_projection(steo):
    facts = steo.normalize(_STEO_RAW)
    past = [f for f in facts if f.period == date(2023, 6, 1)]
    assert past, "no past-period record found"
    assert all(not f.is_projection for f in past)
    assert all(f.scenario is None for f in past)


def test_steo_future_record_is_projection(steo):
    facts = steo.normalize(_STEO_RAW)
    future = [f for f in facts if f.period > date(2024, 1, 1)]
    assert future, "no future-period record found"
    assert all(f.is_projection for f in future)


def test_steo_projection_has_scenario(steo):
    facts = steo.normalize(_STEO_RAW)
    projections = [f for f in facts if f.is_projection]
    assert projections
    assert all(f.scenario == "reference" for f in projections)


def test_steo_validate_ok(steo):
    facts = steo.normalize(_STEO_RAW)
    report = steo.validate(facts)
    assert report.ok, report.errors


def test_protocol_conformance_steo(steo):
    assert isinstance(steo, SourceConnector)


# ---------------------------------------------------------------------------
# EIAIEOConnector
# ---------------------------------------------------------------------------

@pytest.fixture
def ieo(monkeypatch):
    _set_key(monkeypatch)
    return EIAIEOConnector()


def test_ieo_all_records_are_projections(ieo):
    facts = ieo.normalize(_IEO_RAW)
    assert facts
    assert all(f.is_projection for f in facts)


def test_ieo_scenario_label_present(ieo):
    facts = ieo.normalize(_IEO_RAW)
    assert all(f.scenario for f in facts), [f.scenario for f in facts]


def test_ieo_scenarios_mapped(ieo):
    facts = ieo.normalize(_IEO_RAW)
    scenarios = {f.scenario for f in facts}
    assert "reference" in scenarios
    assert "high_economic_growth" in scenarios


def test_ieo_units_are_twh(ieo):
    facts = ieo.normalize(_IEO_RAW)
    assert all(f.unit == "TWh" for f in facts)


def test_ieo_value_roundtrip(ieo):
    # "bill kWh" → TWh factor is 1.0; 9000 bill kWh must stay 9000 TWh.
    facts = ieo.normalize(_IEO_RAW)
    coal = [f for f in facts if f.energy_type.value == "coal"]
    assert coal
    assert abs(coal[0].value - 9000.0) < 0.001


def test_ieo_metric_is_electricity(ieo):
    facts = ieo.normalize(_IEO_RAW)
    from geip.core.schema import MetricFamily
    assert all(f.metric_family == MetricFamily.ELECTRICITY for f in facts)


def test_ieo_validate_ok(ieo):
    facts = ieo.normalize(_IEO_RAW)
    report = ieo.validate(facts)
    assert report.ok, report.errors


def test_protocol_conformance_ieo(ieo):
    assert isinstance(ieo, SourceConnector)


# ---------------------------------------------------------------------------
# Unit conversion — _to_twh
# ---------------------------------------------------------------------------

def test_to_twh_billion_kwh():
    assert _to_twh(1.0, "billion kWh") == 1.0


def test_to_twh_bill_kwh():
    # IEO API uses "bill kWh" as the unit string for billion kilowatt-hours
    assert _to_twh(1.0, "bill kWh") == 1.0


def test_to_twh_bkwh():
    # EIA API v2 uses "BKWH" as the unit code for billion kilowatt-hours
    assert _to_twh(1.0, "BKWH") == 1.0


def test_to_twh_quad_btu():
    assert abs(_to_twh(1.0, "quad BTU") - 293.07153) < 0.001


def test_to_twh_qbtu():
    # EIA API v2 uses "QBTU" as the unit code for quadrillion Btu
    assert abs(_to_twh(1.0, "QBTU") - 293.07153) < 0.001


def test_to_twh_unknown_unit_raises():
    with pytest.raises(ValueError, match="Unrecognized EIA unit"):
        _to_twh(1.0, "furlong-fortnights")
