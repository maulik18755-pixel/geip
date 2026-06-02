"""Phase 0 regression spine.

Locks the hand-verified World 2024 electricity mix. Any pipeline change that
perturbs these numbers — or re-introduces the biofuel double-count — fails CI.

This is the GEIP equivalent of an analytically-derived anchor: the numbers were
verified by inspection (sum reconciles to the reported total within rounding)
before being frozen here.
"""
import json
from datetime import date
from pathlib import Path

import pytest

from geip.analytics.reconcile import electricity_mix, mix_total, reconcile
from geip.connectors.owid import OWIDConnector
from geip.core.schema import CANONICAL_UNIT, EnergyType, FactRecord, MetricFamily

DATA = Path(__file__).parents[1] / "data" / "owid-energy-data.csv"
SPINE = Path(__file__).parent / "spine_world_electricity_2024.json"
TOL_TWH = 0.5  # rounding tolerance


@pytest.fixture(scope="module")
def facts() -> list[FactRecord]:
    conn = OWIDConnector(DATA)
    raw = conn.fetch(since=None)
    facts = conn.normalize(raw)
    report = conn.validate(facts)
    assert report.ok, report.errors
    return facts


@pytest.fixture(scope="module")
def spine() -> dict:
    return json.loads(SPINE.read_text())


def test_spine_reconciles(facts, spine):
    mix = electricity_mix(facts, "World", 2024)
    result = reconcile(mix, spine["reported_total_TWh"])
    assert result["gap_TWh"] < TOL_TWH, result


def test_per_source_matches_spine(facts, spine):
    mix = electricity_mix(facts, "World", 2024)
    expected = spine["by_source_TWh"]
    name_map = {
        EnergyType.COAL: "coal", EnergyType.GAS: "gas", EnergyType.OIL: "oil",
        EnergyType.NUCLEAR: "nuclear", EnergyType.HYDRO: "hydro",
        EnergyType.SOLAR: "solar", EnergyType.WIND: "wind",
        EnergyType.OTHER_RENEWABLE: "other_renewable_incl_biofuel",
    }
    for etype, key in name_map.items():
        assert abs(mix[etype] - expected[key]) < TOL_TWH, (etype, mix[etype], expected[key])


def test_biofuel_not_double_counted(facts):
    """Guard: no separate biofuel record exists in the emitted mix."""
    biofuel_records = [f for f in facts if f.energy_type.value == "biofuel"]
    assert biofuel_records == [], "biofuel must be folded into other_renewable, not emitted"


def test_unit_invariant_enforced():
    """Constructing a fact with a non-canonical unit must raise."""
    with pytest.raises(ValueError):
        FactRecord(
            source_id="x", geography="World", energy_type=EnergyType.SOLAR,
            metric="electricity_generation", metric_family=MetricFamily.ELECTRICITY,
            period=date(2024, 1, 1), period_type="yearly",
            value=1.0, unit="GWh",  # wrong: canonical is TWh
            vintage=date(2024, 1, 1), pull_ts=__import__("datetime").datetime.now(),
        )


def test_canonical_unit_for_electricity():
    assert CANONICAL_UNIT[MetricFamily.ELECTRICITY] == "TWh"
