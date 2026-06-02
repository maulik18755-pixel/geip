"""Core data model for GEIP.

A single fact table (`FactRecord`) plus controlled vocabularies for the
dimensions. Every numeric value in the platform is one FactRecord, fully
self-describing: who said it, about what, when, in what unit, and which
publication vintage it came from.

Design invariants (enforced elsewhere, declared here):
  - metric_family is NEVER mixed in aggregation (primary_energy vs electricity).
  - every value carries an explicit unit.
  - every value carries a vintage (source publication date) and pull_ts.
  - projections are flagged and carry a scenario label.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional


class MetricFamily(str, Enum):
    """Families that must never be summed across each other."""
    PRIMARY_ENERGY = "primary_energy"
    ELECTRICITY = "electricity"
    PRICE = "price"
    EMISSIONS = "emissions"
    CAPACITY = "capacity"
    PIPELINE = "pipeline"


class EnergyType(str, Enum):
    OIL = "oil"
    GAS = "gas"
    COAL = "coal"
    NUCLEAR = "nuclear"
    HYDRO = "hydro"
    SOLAR = "solar"
    WIND = "wind"
    OTHER_RENEWABLE = "other_renewable"  # NOTE: includes biofuel/bioenergy in OWID
    TOTAL = "total"


# Canonical unit per metric family. normalize() must convert to these
# BEFORE any cross-source arithmetic happens.
CANONICAL_UNIT: dict[MetricFamily, str] = {
    MetricFamily.PRIMARY_ENERGY: "TWh",
    MetricFamily.ELECTRICITY: "TWh",
    MetricFamily.PRICE: "USD",          # context (per bbl / per MWh) lives in `metric`
    MetricFamily.EMISSIONS: "MtCO2",
    MetricFamily.CAPACITY: "GW",
    MetricFamily.PIPELINE: "GW",
}


@dataclass(frozen=True)
class FactRecord:
    source_id: str
    geography: str            # "World", country name, or region
    energy_type: EnergyType
    metric: str               # e.g. "electricity_generation", "consumption"
    metric_family: MetricFamily
    period: date              # observation period (year -> Jan 1)
    period_type: str          # "yearly" | "monthly" | "hourly"
    value: float
    unit: str
    vintage: date             # source publication / release date
    pull_ts: datetime
    is_projection: bool = False
    scenario: Optional[str] = None

    def __post_init__(self) -> None:
        if self.unit != CANONICAL_UNIT[self.metric_family]:
            raise ValueError(
                f"Unit '{self.unit}' is not canonical for {self.metric_family.value} "
                f"(expected '{CANONICAL_UNIT[self.metric_family]}'). Normalize before constructing."
            )
        if self.is_projection and not self.scenario:
            raise ValueError("Projection records must carry a scenario label.")


# Reconciliation rules discovered during data inspection. These are facts about
# the SOURCE data, not arbitrary choices. Encoded so no agent re-introduces a
# known double-count.
RECONCILIATION_RULES: dict[str, str] = {
    "owid_biofuel_subset": (
        "In OWID, biofuel_electricity is a SUBSET of other_renewable_electricity. "
        "Do NOT add biofuel separately when summing the electricity mix."
    ),
}
