"""Tests for the reconciliation engine and discrepancy log.

The spec requirement covered here:
  "Known-divergent series must raise a discrepancy flag."

All tests are pure-Python — no I/O, no connectors, no network.
FactRecords are constructed directly using _fact() below.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

import pytest

from geip.analytics.engine import (
    DiscrepancyRecord,
    ReconciliationEngine,
    SeriesKey,
    SPINE_SOURCE_ID,
)
from geip.core.schema import CANONICAL_UNIT, EnergyType, FactRecord, MetricFamily

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, tzinfo=timezone.utc)


def _fact(
    *,
    source_id: str = SPINE_SOURCE_ID,
    geography: str = "World",
    energy_type: EnergyType = EnergyType.COAL,
    metric: str = "electricity_generation",
    metric_family: MetricFamily = MetricFamily.ELECTRICITY,
    year: int = 2022,
    value: float = 1000.0,
    vintage_year: int = 2023,
    is_projection: bool = False,
    scenario: Optional[str] = None,
) -> FactRecord:
    return FactRecord(
        source_id=source_id,
        geography=geography,
        energy_type=energy_type,
        metric=metric,
        metric_family=metric_family,
        period=date(year, 1, 1),
        period_type="yearly",
        value=value,
        unit=CANONICAL_UNIT[metric_family],
        vintage=date(vintage_year, 1, 1),
        pull_ts=_NOW,
        is_projection=is_projection,
        scenario=scenario,
    )


@pytest.fixture
def engine():
    return ReconciliationEngine()


# ---------------------------------------------------------------------------
# 1. No overlap → empty log
# ---------------------------------------------------------------------------

def test_no_overlap_returns_empty(engine):
    spine = [_fact(energy_type=EnergyType.COAL)]
    challenger = [_fact(source_id="eia_international", energy_type=EnergyType.SOLAR)]
    assert engine.compare(spine, challenger) == []


# ---------------------------------------------------------------------------
# 2–3. Flag logic
# ---------------------------------------------------------------------------

def test_exact_match_not_flagged(engine):
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1000.0)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].abs_delta == 0.0
    assert log[0].pct_delta == pytest.approx(0.0)
    assert not log[0].flagged


def test_within_threshold_not_flagged(engine):
    # 3% divergence, default threshold = 5%
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1030.0)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert not log[0].flagged
    assert log[0].pct_delta == pytest.approx(3.0)


def test_exceeds_threshold_flagged(engine):
    # Spec requirement: known-divergent series must raise a discrepancy flag.
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1110.0)]  # +11%
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].flagged
    assert log[0].pct_delta == pytest.approx(11.0)


# ---------------------------------------------------------------------------
# 4–5. pct_delta sign
# ---------------------------------------------------------------------------

def test_pct_delta_positive_when_challenger_higher(engine):
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1200.0)]
    log = engine.compare(spine, challenger)
    assert log[0].pct_delta == pytest.approx(20.0)


def test_pct_delta_negative_when_challenger_lower(engine):
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=800.0)]
    log = engine.compare(spine, challenger)
    assert log[0].pct_delta == pytest.approx(-20.0)


# ---------------------------------------------------------------------------
# 6. Flag is symmetric around zero
# ---------------------------------------------------------------------------

def test_flagged_both_negative_and_positive_divergence(engine):
    spine = [
        _fact(energy_type=EnergyType.COAL, value=1000.0),
        _fact(energy_type=EnergyType.GAS,  value=1000.0),
    ]
    challenger = [
        _fact(source_id="eia_international", energy_type=EnergyType.COAL, value=1100.0),  # +10%
        _fact(source_id="eia_international", energy_type=EnergyType.GAS,  value=900.0),   # -10%
    ]
    log = engine.compare(spine, challenger)
    assert all(d.flagged for d in log), [(d.energy_type, d.pct_delta) for d in log]


# ---------------------------------------------------------------------------
# 7. Projections are excluded
# ---------------------------------------------------------------------------

def test_projection_challenger_excluded(engine):
    spine = [_fact(value=1000.0)]
    proj = [_fact(source_id="eia_steo", value=1500.0, is_projection=True, scenario="reference")]
    assert engine.compare(spine, proj) == []


def test_projection_spine_excluded(engine):
    # A projection in the spine pool should not appear in the index.
    spine = [
        _fact(value=1000.0),  # historical — used
        _fact(value=9999.0, is_projection=True, scenario="s"),  # projection — ignored
    ]
    challenger = [_fact(source_id="eia_international", value=1000.0)]
    log = engine.compare(spine, challenger)
    # The historical spine value (1000) is used, not the projection (9999).
    assert log[0].spine_value == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 8. Spine source excluded from challenger
# ---------------------------------------------------------------------------

def test_spine_source_not_compared_against_itself(engine):
    # OWID facts passed as challenger must be filtered out.
    spine = [_fact(value=1000.0)]
    owid_as_challenger = [_fact(source_id=SPINE_SOURCE_ID, value=2000.0)]
    assert engine.compare(spine, owid_as_challenger) == []


# ---------------------------------------------------------------------------
# 9. Latest-vintage rule
# ---------------------------------------------------------------------------

def test_latest_vintage_wins(engine):
    spine = [_fact(value=1000.0, vintage_year=2023)]
    old = _fact(source_id="eia_international", value=900.0, vintage_year=2022)
    new = _fact(source_id="eia_international", value=950.0, vintage_year=2024)
    log = engine.compare(spine, [old, new])
    assert len(log) == 1
    assert log[0].challenger_value == pytest.approx(950.0)
    assert log[0].challenger_vintage == date(2024, 1, 1)


def test_latest_vintage_for_spine_too(engine):
    old_spine = _fact(value=800.0, vintage_year=2022)
    new_spine = _fact(value=1000.0, vintage_year=2023)
    challenger = [_fact(source_id="eia_international", value=1050.0)]
    log = engine.compare([old_spine, new_spine], challenger)
    assert log[0].spine_value == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# 10. Spine silence → no discrepancy
# ---------------------------------------------------------------------------

def test_spine_silence_no_discrepancy(engine):
    # Spine has coal; challenger has solar. No overlap → no records.
    spine = [_fact(energy_type=EnergyType.COAL, value=1000.0)]
    challenger = [_fact(source_id="eia_international", energy_type=EnergyType.SOLAR, value=500.0)]
    assert engine.compare(spine, challenger) == []


# ---------------------------------------------------------------------------
# 11. Multiple challenger sources
# ---------------------------------------------------------------------------

def test_multiple_challengers_produce_separate_records(engine):
    spine = [_fact(value=1000.0)]
    eia = _fact(source_id="eia_international", value=1100.0)
    ember = _fact(source_id="ember", value=1050.0)
    log = engine.compare(spine, [eia, ember])
    assert len(log) == 2
    sources = {d.challenger_source_id for d in log}
    assert sources == {"eia_international", "ember"}


# ---------------------------------------------------------------------------
# 12. Spine zero → pct_delta None, never flagged
# ---------------------------------------------------------------------------

def test_spine_zero_pct_delta_none(engine):
    spine = [_fact(value=0.0)]
    challenger = [_fact(source_id="eia_international", value=500.0)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].pct_delta is None
    assert not log[0].flagged
    assert log[0].abs_delta == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# 13. Custom threshold
# ---------------------------------------------------------------------------

def test_custom_threshold_tighter():
    tight_engine = ReconciliationEngine(threshold_pct=1.0)
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1025.0)]  # 2.5%
    log = tight_engine.compare(spine, challenger)
    assert log[0].flagged  # 2.5% > 1.0%
    assert log[0].threshold_pct == 1.0


def test_custom_threshold_looser():
    loose_engine = ReconciliationEngine(threshold_pct=20.0)
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1150.0)]  # 15%
    log = loose_engine.compare(spine, challenger)
    assert not log[0].flagged  # 15% < 20%


def test_invalid_threshold_raises():
    with pytest.raises(ValueError, match="threshold_pct"):
        ReconciliationEngine(threshold_pct=0.0)
    with pytest.raises(ValueError, match="threshold_pct"):
        ReconciliationEngine(threshold_pct=-5.0)


# ---------------------------------------------------------------------------
# 14. flagged() filter helper
# ---------------------------------------------------------------------------

def test_flagged_filter_returns_only_flagged(engine):
    spine = [
        _fact(energy_type=EnergyType.COAL, value=1000.0),
        _fact(energy_type=EnergyType.GAS,  value=1000.0),
    ]
    challenger = [
        _fact(source_id="eia_international", energy_type=EnergyType.COAL, value=1001.0),  # 0.1% — ok
        _fact(source_id="eia_international", energy_type=EnergyType.GAS,  value=1200.0),  # 20% — flagged
    ]
    log = engine.compare(spine, challenger)
    flagged = engine.flagged(log)
    assert len(log) == 2
    assert len(flagged) == 1
    assert flagged[0].energy_type is EnergyType.GAS


# ---------------------------------------------------------------------------
# 15. summary() aggregation
# ---------------------------------------------------------------------------

def test_summary_counts(engine):
    spine = [
        _fact(energy_type=EnergyType.COAL, value=1000.0),
        _fact(energy_type=EnergyType.GAS,  value=1000.0),
    ]
    challenger = [
        _fact(source_id="eia_international", energy_type=EnergyType.COAL, value=1001.0),
        _fact(source_id="eia_international", energy_type=EnergyType.GAS,  value=1200.0),
    ]
    log = engine.compare(spine, challenger)
    s = engine.summary(log)
    assert s["total"] == 2
    assert s["flagged"] == 1
    assert s["flag_rate_pct"] == 50.0
    assert s["by_source"]["eia_international"]["total"] == 2
    assert s["by_source"]["eia_international"]["flagged"] == 1


def test_summary_empty_log(engine):
    s = engine.summary([])
    assert s["total"] == 0
    assert s["flagged"] == 0


# ---------------------------------------------------------------------------
# 16. Provenance fields on DiscrepancyRecord
# ---------------------------------------------------------------------------

def test_discrepancy_record_provenance(engine):
    spine = [_fact(value=1000.0, vintage_year=2023)]
    challenger = [_fact(source_id="ember", value=1100.0, vintage_year=2024)]
    log = engine.compare(spine, challenger)
    d = log[0]
    assert d.spine_source_id == SPINE_SOURCE_ID
    assert d.spine_vintage == date(2023, 1, 1)
    assert d.challenger_source_id == "ember"
    assert d.challenger_vintage == date(2024, 1, 1)
    assert d.geography == "World"
    assert d.period == date(2022, 1, 1)
    assert d.threshold_pct == engine.threshold_pct
    assert d.detected_at.tzinfo is not None  # timezone-aware


# ---------------------------------------------------------------------------
# 17. Different metric families are NEVER cross-compared
# ---------------------------------------------------------------------------

def test_different_metric_families_not_compared(engine):
    # PRIMARY_ENERGY coal and ELECTRICITY coal for same geo/year — different keys.
    spine_elec = _fact(
        metric_family=MetricFamily.ELECTRICITY,
        metric="electricity_generation",
        value=1000.0,
    )
    challenger_primary = _fact(
        source_id="eia_international",
        metric_family=MetricFamily.PRIMARY_ENERGY,
        metric="primary_energy_consumption",
        value=5000.0,
    )
    log = engine.compare([spine_elec], [challenger_primary])
    assert log == [], "Primary energy and electricity must never be cross-compared"


def test_same_family_same_metric_compared(engine):
    spine = [_fact(metric_family=MetricFamily.ELECTRICITY, metric="electricity_generation", value=1000.0)]
    challenger = [_fact(source_id="ember", metric_family=MetricFamily.ELECTRICITY, metric="electricity_generation", value=1200.0)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1


# ---------------------------------------------------------------------------
# 18. SeriesKey.series property round-trip
# ---------------------------------------------------------------------------

def test_series_property(engine):
    spine = [_fact(value=1000.0)]
    challenger = [_fact(source_id="eia_international", value=1100.0)]
    d = engine.compare(spine, challenger)[0]
    sk = d.series
    assert sk.geography == d.geography
    assert sk.energy_type is d.energy_type
    assert sk.metric == d.metric
    assert sk.metric_family is d.metric_family
    assert sk.period == d.period


# ---------------------------------------------------------------------------
# 19. Empty inputs
# ---------------------------------------------------------------------------

def test_empty_spine_returns_empty(engine):
    challenger = [_fact(source_id="eia_international", value=1000.0)]
    assert engine.compare([], challenger) == []


def test_empty_challenger_returns_empty(engine):
    spine = [_fact(value=1000.0)]
    assert engine.compare(spine, []) == []


# ---------------------------------------------------------------------------
# 20. Absolute-magnitude floor (min_abs_twh)
# ---------------------------------------------------------------------------

def test_both_below_floor_not_flagged(engine):
    # 0.5 vs 0.8 TWh → 60% delta, but both < 1.0 TWh floor → flag suppressed.
    spine = [_fact(value=0.5)]
    challenger = [_fact(source_id="eia_international", value=0.8)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].below_floor is True
    assert log[0].flagged is False
    assert log[0].pct_delta == pytest.approx(60.0)


def test_below_floor_record_still_emitted(engine):
    # below_floor records must appear in compare() output — not silently dropped.
    spine = [_fact(value=0.1)]
    challenger = [_fact(source_id="eia_international", value=0.2)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].below_floor is True


def test_one_above_one_below_evaluated_normally(engine):
    # spine below floor, challenger above — floor does NOT apply; normal pct logic runs.
    spine = [_fact(value=0.5)]
    challenger = [_fact(source_id="eia_international", value=2.0)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].below_floor is False
    assert log[0].flagged is True   # (2.0 - 0.5) / 0.5 * 100 = 300% > 5%


def test_both_above_floor_unaffected(engine):
    # Both well above floor → normal flag behaviour unchanged.
    spine = [_fact(value=100.0)]
    challenger = [_fact(source_id="eia_international", value=200.0)]
    log = engine.compare(spine, challenger)
    assert len(log) == 1
    assert log[0].below_floor is False
    assert log[0].flagged is True   # 100% > 5%


def test_custom_min_abs_twh():
    engine5 = ReconciliationEngine(min_abs_twh=5.0)
    spine = [_fact(value=3.0)]
    challenger = [_fact(source_id="eia_international", value=4.0)]
    log = engine5.compare(spine, challenger)
    assert log[0].below_floor is True   # both < 5.0
    assert log[0].flagged is False      # 33% delta suppressed by floor


def test_floor_zero_disables_suppression():
    # min_abs_twh=0.0 means no floor: tiny-value pct deltas are still flagged.
    engine0 = ReconciliationEngine(min_abs_twh=0.0)
    spine = [_fact(value=0.1)]
    challenger = [_fact(source_id="eia_international", value=0.2)]
    log = engine0.compare(spine, challenger)
    assert log[0].below_floor is False
    assert log[0].flagged is True   # 100% > 5%, no floor to suppress it


def test_floor_suppressed_filter(engine):
    spine = [
        _fact(energy_type=EnergyType.COAL, value=0.3),    # below floor
        _fact(energy_type=EnergyType.GAS,  value=1000.0), # above floor
    ]
    challenger = [
        _fact(source_id="eia_international", energy_type=EnergyType.COAL, value=0.5),    # below floor
        _fact(source_id="eia_international", energy_type=EnergyType.GAS,  value=1200.0), # above floor, flagged
    ]
    log = engine.compare(spine, challenger)
    assert len(log) == 2
    suppressed = engine.floor_suppressed(log)
    assert len(suppressed) == 1
    assert suppressed[0].energy_type is EnergyType.COAL


def test_summary_reports_below_floor(engine):
    spine = [
        _fact(energy_type=EnergyType.COAL, value=0.3),
        _fact(energy_type=EnergyType.GAS,  value=1000.0),
    ]
    challenger = [
        _fact(source_id="eia_international", energy_type=EnergyType.COAL, value=0.5),
        _fact(source_id="eia_international", energy_type=EnergyType.GAS,  value=1200.0),
    ]
    log = engine.compare(spine, challenger)
    s = engine.summary(log)
    assert s["total"] == 2
    assert s["below_floor"] == 1
    assert s["flagged"] == 1
    assert s["by_source"]["eia_international"]["below_floor"] == 1


def test_invalid_min_abs_twh_raises():
    with pytest.raises(ValueError, match="min_abs_twh"):
        ReconciliationEngine(min_abs_twh=-1.0)
