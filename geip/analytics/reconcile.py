"""Reconciliation: sum the electricity mix the RIGHT way.

The one rule that matters here: the eight energy types we emit already form a
non-overlapping partition because biofuel is folded inside OTHER_RENEWABLE and
never emitted separately. So a plain sum is correct. This module exists to make
that explicit and to provide the gap-check used by both the analytics layer and
the regression test.
"""
from __future__ import annotations

from datetime import date

from geip.core.schema import EnergyType, FactRecord, MetricFamily

# The non-overlapping partition of the electricity mix.
_MIX_TYPES = {
    EnergyType.COAL, EnergyType.GAS, EnergyType.OIL, EnergyType.NUCLEAR,
    EnergyType.HYDRO, EnergyType.SOLAR, EnergyType.WIND, EnergyType.OTHER_RENEWABLE,
}


def electricity_mix(
    facts: list[FactRecord], geography: str, year: int
) -> dict[EnergyType, float]:
    """Return {energy_type: TWh} for one geography/year electricity mix."""
    period = date(year, 1, 1)
    out: dict[EnergyType, float] = {}
    for f in facts:
        if (
            f.metric_family is MetricFamily.ELECTRICITY
            and f.geography == geography
            and f.period == period
            and f.energy_type in _MIX_TYPES
        ):
            out[f.energy_type] = out.get(f.energy_type, 0.0) + f.value
    return out


def mix_total(mix: dict[EnergyType, float]) -> float:
    """Total generation = simple sum (partition is non-overlapping)."""
    return sum(mix.values())


def reconcile(mix: dict[EnergyType, float], reported_total: float) -> dict:
    """Compare computed sum against an independently reported total."""
    computed = mix_total(mix)
    gap = abs(computed - reported_total)
    return {
        "computed_sum_TWh": round(computed, 1),
        "reported_total_TWh": round(reported_total, 1),
        "gap_TWh": round(gap, 1),
        "gap_pct": round(gap / reported_total * 100, 3) if reported_total else None,
    }
