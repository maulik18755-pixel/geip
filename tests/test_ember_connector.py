"""Unit tests for EmberConnector.normalize() — no network calls.

Critical invariants tested:
  - Bioenergy is folded into OTHER_RENEWABLE, not emitted as a separate type.
  - Aggregate subcategories (Total generation, Fossil, Renewables …) are skipped.
  - Generation records carry unit="TWh"; capacity records carry unit="GW".
  - No record is ever is_projection=True (Ember is historical only).
  - Missing EMBER_API_KEY raises before any HTTP call.
  - Unexpected unit string from Ember raises ValueError in normalize().
  - Orphan bioenergy (bioenergy row with no paired Other renewables row)
    is still emitted as OTHER_RENEWABLE rather than silently dropped.
"""
from __future__ import annotations

from datetime import date

import pytest

from geip.connectors.base import SourceConnector
from geip.connectors.ember import EmberConnector, _BIOENERGY_LABEL
from geip.core.schema import EnergyType, MetricFamily

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBER_API_KEY", "TEST_KEY")


@pytest.fixture
def ember(monkeypatch):
    _set_key(monkeypatch)
    return EmberConnector()


# Full representative payload: all 8 fuel types, aggregates, and capacity rows.
_EMBER_RAW: list[dict] = [
    # Individual fuel generation
    {"country_or_region": "World", "year": 2023, "category": "Fossil",     "subcategory": "Coal",           "variable": "Generation",         "unit": "TWh", "value": "10000"},
    {"country_or_region": "World", "year": 2023, "category": "Fossil",     "subcategory": "Gas",            "variable": "Generation",         "unit": "TWh", "value": "6500"},
    {"country_or_region": "World", "year": 2023, "category": "Fossil",     "subcategory": "Other fossil",   "variable": "Generation",         "unit": "TWh", "value": "800"},
    {"country_or_region": "World", "year": 2023, "category": "Low-carbon", "subcategory": "Nuclear",        "variable": "Generation",         "unit": "TWh", "value": "2700"},
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Hydro",          "variable": "Generation",         "unit": "TWh", "value": "4400"},
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Solar",          "variable": "Generation",         "unit": "TWh", "value": "1800"},
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Wind",           "variable": "Generation",         "unit": "TWh", "value": "2200"},
    # Other renewables (exclusive of bioenergy in Ember's schema)
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Other renewables", "variable": "Generation",       "unit": "TWh", "value": "400"},
    # Bioenergy — must be folded into OTHER_RENEWABLE, not emitted separately
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Bioenergy",      "variable": "Generation",         "unit": "TWh", "value": "600"},
    # Aggregates — must be skipped to prevent double-counting
    {"country_or_region": "World", "year": 2023, "category": "Total",      "subcategory": "Total generation","variable": "Generation",        "unit": "TWh", "value": "29400"},
    {"country_or_region": "World", "year": 2023, "category": "Fossil",     "subcategory": "Fossil",         "variable": "Generation",         "unit": "TWh", "value": "17300"},
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Renewables",     "variable": "Generation",         "unit": "TWh", "value": "9400"},
    {"country_or_region": "World", "year": 2023, "category": "Low-carbon", "subcategory": "Low-carbon",     "variable": "Generation",         "unit": "TWh", "value": "11800"},
    # Capacity
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Solar",          "variable": "Installed capacity", "unit": "GW",  "value": "1600"},
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Wind",           "variable": "Installed capacity", "unit": "GW",  "value": "900"},
]

# Payload where Bioenergy has NO paired "Other renewables" row (orphan bioenergy).
_ORPHAN_BIO_RAW: list[dict] = [
    {"country_or_region": "Iceland", "year": 2023, "category": "Renewables", "subcategory": "Bioenergy",
     "variable": "Generation", "unit": "TWh", "value": "2.5"},
]

# Payload with a bad unit to test the hard-error path.
_BAD_UNIT_RAW: list[dict] = [
    {"country_or_region": "World", "year": 2023, "category": "Renewables", "subcategory": "Solar",
     "variable": "Generation", "unit": "GWh", "value": "1000"},  # GWh is wrong for Generation
]


# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------

def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("EMBER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="EMBER_API_KEY"):
        EmberConnector()


# ---------------------------------------------------------------------------
# Bioenergy folding — the core invariant
# ---------------------------------------------------------------------------

def test_bioenergy_folded_into_other_renewable(ember):
    facts = ember.normalize(_EMBER_RAW)
    other = [f for f in facts
             if f.energy_type is EnergyType.OTHER_RENEWABLE
             and f.metric_family is MetricFamily.ELECTRICITY
             and f.geography == "World"
             and f.period.year == 2023]
    assert len(other) == 1, f"Expected exactly one OTHER_RENEWABLE generation record, got {len(other)}"
    # 400 TWh (Other renewables) + 600 TWh (Bioenergy) = 1000 TWh
    assert abs(other[0].value - 1000.0) < 0.01, f"Expected 1000.0 TWh, got {other[0].value}"


def test_no_standalone_bioenergy_record(ember):
    facts = ember.normalize(_EMBER_RAW)
    # EnergyType.BIOENERGY does not exist in the schema; confirm nothing leaks through
    # by checking no record has a value_type outside the known enum members.
    known_types = set(EnergyType)
    for f in facts:
        assert f.energy_type in known_types


def test_orphan_bioenergy_still_emitted(ember):
    """Bioenergy with no paired Other renewables row must not be silently dropped."""
    facts = ember.normalize(_ORPHAN_BIO_RAW)
    assert len(facts) == 1
    assert facts[0].energy_type is EnergyType.OTHER_RENEWABLE
    assert abs(facts[0].value - 2.5) < 0.01


# ---------------------------------------------------------------------------
# Aggregate skipping
# ---------------------------------------------------------------------------

def test_aggregate_subcategories_skipped(ember):
    facts = ember.normalize(_EMBER_RAW)
    # Aggregate rows carry values like 29400, 17300, 9400, 11800 — none should appear.
    values = {f.value for f in facts}
    for aggregate_val in (29400.0, 17300.0, 9400.0, 11800.0):
        assert aggregate_val not in values, f"Aggregate value {aggregate_val} leaked into facts"


def test_eight_generation_types_emitted(ember):
    gen_facts = [f for f in ember.normalize(_EMBER_RAW)
                 if f.metric == "electricity_generation"]
    etypes = {f.energy_type for f in gen_facts}
    expected = {
        EnergyType.COAL, EnergyType.GAS, EnergyType.OIL,
        EnergyType.NUCLEAR, EnergyType.HYDRO, EnergyType.SOLAR,
        EnergyType.WIND, EnergyType.OTHER_RENEWABLE,
    }
    assert etypes == expected


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def test_generation_units_are_twh(ember):
    facts = ember.normalize(_EMBER_RAW)
    gen = [f for f in facts if f.metric_family is MetricFamily.ELECTRICITY]
    assert gen, "no electricity generation facts"
    assert all(f.unit == "TWh" for f in gen), [f.unit for f in gen]


def test_capacity_units_are_gw(ember):
    facts = ember.normalize(_EMBER_RAW)
    cap = [f for f in facts if f.metric_family is MetricFamily.CAPACITY]
    assert cap, "no capacity facts"
    assert all(f.unit == "GW" for f in cap), [f.unit for f in cap]


def test_wrong_unit_raises(ember):
    with pytest.raises(ValueError, match="GWh"):
        ember.normalize(_BAD_UNIT_RAW)


# ---------------------------------------------------------------------------
# Projection invariant
# ---------------------------------------------------------------------------

def test_no_projections(ember):
    facts = ember.normalize(_EMBER_RAW)
    assert all(not f.is_projection for f in facts)
    assert all(f.scenario is None for f in facts)


# ---------------------------------------------------------------------------
# Values and provenance
# ---------------------------------------------------------------------------

def test_coal_generation_value(ember):
    facts = ember.normalize(_EMBER_RAW)
    coal = [f for f in facts if f.energy_type is EnergyType.COAL
            and f.metric_family is MetricFamily.ELECTRICITY]
    assert coal
    assert abs(coal[0].value - 10000.0) < 0.01


def test_capacity_values(ember):
    facts = ember.normalize(_EMBER_RAW)
    solar_cap = [f for f in facts
                 if f.energy_type is EnergyType.SOLAR
                 and f.metric_family is MetricFamily.CAPACITY]
    assert solar_cap
    assert abs(solar_cap[0].value - 1600.0) < 0.01


def test_period_is_jan_1(ember):
    facts = ember.normalize(_EMBER_RAW)
    for f in facts:
        assert f.period.month == 1 and f.period.day == 1, f.period


def test_source_id(ember):
    facts = ember.normalize(_EMBER_RAW)
    assert all(f.source_id == "ember" for f in facts)


def test_skips_empty_values(ember):
    raw = [dict(_EMBER_RAW[0], value="")]
    assert ember.normalize(raw) == []


def test_skips_missing_year(ember):
    raw = [dict(_EMBER_RAW[0], year=None)]
    assert ember.normalize(raw) == []


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------

def test_validate_ok(ember):
    facts = ember.normalize(_EMBER_RAW)
    report = ember.validate(facts)
    assert report.ok, report.errors


def test_validate_catches_negative(ember, monkeypatch):
    facts = ember.normalize(_EMBER_RAW)
    # Bypass frozen dataclass to inject a negative value via object.__setattr__
    bad = facts[0]
    from geip.core.schema import FactRecord
    bad_fact = FactRecord(
        source_id=bad.source_id, geography=bad.geography,
        energy_type=bad.energy_type, metric=bad.metric,
        metric_family=bad.metric_family, period=bad.period,
        period_type=bad.period_type, value=-1.0, unit=bad.unit,
        vintage=bad.vintage, pull_ts=bad.pull_ts,
    )
    report = ember.validate([bad_fact])
    assert not report.ok
    assert report.n_errors == 1


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_protocol_conformance(ember):
    assert isinstance(ember, SourceConnector)


def test_cadence_label(ember):
    assert ember.cadence.label == "twice_monthly"


def test_source_id_attribute(ember):
    assert ember.source_id == "ember"
