"""OWID connector — the canonical spine.

Reads the Our World in Data harmonized energy CSV and emits the world
electricity-generation mix as FactRecords. This is the reconciliation anchor:
all other sources are compared against OWID, not the reverse.

Kept deliberately small for Phase 0. Extend `_ELEC_COLS` and add primary-energy
/ consumption mappings in Phase 1, but never change the spine semantics without
updating the regression test.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from geip.connectors.base import Cadence, SourceConnector, ValidationReport
from geip.core.schema import EnergyType, FactRecord, MetricFamily

# OWID electricity column -> our EnergyType.
# IMPORTANT: biofuel_electricity is intentionally ABSENT. It is a subset of
# other_renewable_electricity and adding it double-counts (see RECONCILIATION_RULES).
_ELEC_COLS: dict[str, EnergyType] = {
    "coal_electricity": EnergyType.COAL,
    "gas_electricity": EnergyType.GAS,
    "oil_electricity": EnergyType.OIL,
    "nuclear_electricity": EnergyType.NUCLEAR,
    "hydro_electricity": EnergyType.HYDRO,
    "solar_electricity": EnergyType.SOLAR,
    "wind_electricity": EnergyType.WIND,
    "other_renewable_electricity": EnergyType.OTHER_RENEWABLE,
}


class OWIDConnector(SourceConnector):
    source_id = "owid_energy"
    cadence = Cadence(label="irregular", poll_hours=24)
    license = "CC BY (Our World in Data)"

    def __init__(self, csv_path: str | Path, vintage: Optional[date] = None):
        self.csv_path = Path(csv_path)
        # OWID has no per-row publication date; use file mtime as vintage proxy.
        self._vintage = vintage or date.fromtimestamp(self.csv_path.stat().st_mtime)

    def fetch(self, since: Optional[date]) -> pd.DataFrame:
        df = pd.read_csv(self.csv_path)
        if since and self._vintage <= since:
            return df.iloc[0:0]  # nothing newer than what we already have
        return df

    def normalize(self, raw: pd.DataFrame) -> list[FactRecord]:
        if raw.empty:
            return []
        pull_ts = datetime.now(timezone.utc)
        facts: list[FactRecord] = []
        subset = raw[["country", "year", *_ELEC_COLS.keys()]]
        for _, row in subset.iterrows():
            if pd.isna(row["year"]):
                continue
            period = date(int(row["year"]), 1, 1)
            for col, etype in _ELEC_COLS.items():
                val = row[col]
                if pd.isna(val):
                    continue
                facts.append(
                    FactRecord(
                        source_id=self.source_id,
                        geography=row["country"],
                        energy_type=etype,
                        metric="electricity_generation",
                        metric_family=MetricFamily.ELECTRICITY,
                        period=period,
                        period_type="yearly",
                        value=float(val),
                        unit="TWh",
                        vintage=self._vintage,
                        pull_ts=pull_ts,
                    )
                )
        return facts

    def validate(self, facts: list[FactRecord]) -> ValidationReport:
        errors: list[str] = []
        for f in facts:
            if f.value < 0:
                errors.append(f"Negative value: {f.geography} {f.energy_type.value} {f.period}")
            if f.metric_family is not MetricFamily.ELECTRICITY:
                errors.append(f"Unexpected metric_family: {f.metric_family}")
        return ValidationReport(self.source_id, len(facts), len(errors), errors)
