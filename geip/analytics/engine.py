"""Reconciliation engine: compare challenger sources against the OWID spine.

The spine is the set of FactRecords from source_id == "owid_energy". Every
other source is a challenger. For each series where the spine and a challenger
both have a non-projection observation, a DiscrepancyRecord is emitted. Records
where abs(pct_delta) > threshold_pct are flagged for human review.

Rules enforced here (from CLAUDE.md):
  - The spine is never edited or "corrected". OWID is the immutable anchor.
  - Projections (is_projection=True) are never compared against spine values.
  - Series present only in the challenger are silently ignored; spine silence is
    not a discrepancy — it means the series is outside OWID's scope.
  - Every DiscrepancyRecord carries full provenance: spine vintage, challenger
    vintage, detected_at timestamp.
  - PRIMARY_ENERGY and ELECTRICITY are kept separate: SeriesKey includes
    metric_family, so cross-family comparisons can never happen.

Usage:
    engine = ReconciliationEngine(threshold_pct=5.0)
    log = engine.compare(spine_facts=owid_facts, challenger_facts=eia_facts)
    for d in engine.flagged(log):
        alert(d)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from geip.core.schema import EnergyType, FactRecord, MetricFamily

SPINE_SOURCE_ID = "owid_energy"


@dataclass(frozen=True)
class SeriesKey:
    """Uniquely identifies one energy series for cross-source comparison.

    metric_family is included so PRIMARY_ENERGY and ELECTRICITY series
    with the same geography/energy_type/metric/period are never compared.
    """
    geography: str
    energy_type: EnergyType
    metric: str
    metric_family: MetricFamily
    period: date


@dataclass(frozen=True)
class DiscrepancyRecord:
    """One cross-source comparison result.

    pct_delta: signed percentage — positive means challenger is higher than
    spine, negative means lower. None when spine_value == 0 (undefined).
    flagged: True when abs(pct_delta) > threshold_pct. Always False when
    pct_delta is None (absolute-delta thresholding is a Phase 2 extension).
    """
    # Series identity
    geography: str
    energy_type: EnergyType
    metric: str
    metric_family: MetricFamily
    period: date
    # Spine side
    spine_source_id: str
    spine_value: float
    spine_vintage: date
    # Challenger side
    challenger_source_id: str
    challenger_value: float
    challenger_vintage: date
    # Comparison result
    abs_delta: float              # |challenger_value - spine_value|
    pct_delta: Optional[float]    # (challenger - spine) / spine × 100, signed
    flagged: bool                 # abs(pct_delta) > threshold_pct
    threshold_pct: float
    detected_at: datetime

    @property
    def series(self) -> SeriesKey:
        return SeriesKey(
            self.geography, self.energy_type,
            self.metric, self.metric_family, self.period,
        )


class ReconciliationEngine:
    """Compare challenger FactRecords against the OWID spine.

    threshold_pct controls when a discrepancy is flagged. All overlapping
    series are recorded regardless; threshold only governs the flagged
    attribute. This keeps the full audit trail while highlighting what needs
    human review.
    """

    DEFAULT_THRESHOLD_PCT = 5.0

    def __init__(self, threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> None:
        if threshold_pct <= 0:
            raise ValueError(f"threshold_pct must be positive, got {threshold_pct!r}")
        self.threshold_pct = threshold_pct

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compare(
        self,
        spine_facts: list[FactRecord],
        challenger_facts: list[FactRecord],
    ) -> list[DiscrepancyRecord]:
        """Compare challengers against the spine; return the discrepancy log.

        One DiscrepancyRecord is emitted per (SeriesKey, challenger_source_id)
        pair where both the spine and the challenger have a non-projection
        observation. Series present only in the challenger are skipped —
        spine silence is not a discrepancy.

        Multiple challenger sources may be passed in one call; each appears
        with its own challenger_source_id in the output.
        """
        spine_index = self._build_spine_index(spine_facts)
        challenger_index = self._build_challenger_index(challenger_facts)

        detected_at = datetime.now(timezone.utc)
        records: list[DiscrepancyRecord] = []

        for (series_key, challenger_source_id), challenger_fact in sorted(
            challenger_index.items(), key=lambda kv: str(kv[0])
        ):
            spine_fact = spine_index.get(series_key)
            if spine_fact is None:
                continue  # spine is silent on this series

            spine_val = spine_fact.value
            challenger_val = challenger_fact.value
            abs_delta = abs(challenger_val - spine_val)

            if spine_val != 0.0:
                pct_delta: Optional[float] = (
                    (challenger_val - spine_val) / spine_val * 100.0
                )
                flagged = abs(pct_delta) > self.threshold_pct
            else:
                pct_delta = None
                flagged = False

            records.append(DiscrepancyRecord(
                geography=series_key.geography,
                energy_type=series_key.energy_type,
                metric=series_key.metric,
                metric_family=series_key.metric_family,
                period=series_key.period,
                spine_source_id=SPINE_SOURCE_ID,
                spine_value=spine_val,
                spine_vintage=spine_fact.vintage,
                challenger_source_id=challenger_source_id,
                challenger_value=challenger_val,
                challenger_vintage=challenger_fact.vintage,
                abs_delta=abs_delta,
                pct_delta=pct_delta,
                flagged=flagged,
                threshold_pct=self.threshold_pct,
                detected_at=detected_at,
            ))

        return records

    def flagged(self, log: list[DiscrepancyRecord]) -> list[DiscrepancyRecord]:
        """Return only the records that exceeded the threshold."""
        return [d for d in log if d.flagged]

    def summary(self, log: list[DiscrepancyRecord]) -> dict:
        """Aggregate statistics over a discrepancy log."""
        if not log:
            return {"total": 0, "flagged": 0, "flag_rate_pct": 0.0, "by_source": {}}
        n_flagged = sum(1 for d in log if d.flagged)
        by_source: dict[str, dict] = {}
        for d in log:
            s = by_source.setdefault(d.challenger_source_id, {"total": 0, "flagged": 0})
            s["total"] += 1
            if d.flagged:
                s["flagged"] += 1
        return {
            "total": len(log),
            "flagged": n_flagged,
            "flag_rate_pct": round(n_flagged / len(log) * 100, 1),
            "by_source": by_source,
        }

    # ------------------------------------------------------------------
    # Index builders (no I/O, no mutation)
    # ------------------------------------------------------------------

    def _build_spine_index(
        self,
        facts: list[FactRecord],
    ) -> dict[SeriesKey, FactRecord]:
        """Latest-vintage OWID record per SeriesKey. Projections excluded."""
        index: dict[SeriesKey, FactRecord] = {}
        for f in facts:
            if f.source_id != SPINE_SOURCE_ID or f.is_projection:
                continue
            key = SeriesKey(
                f.geography, f.energy_type, f.metric, f.metric_family, f.period,
            )
            existing = index.get(key)
            if existing is None or f.vintage > existing.vintage:
                index[key] = f
        return index

    def _build_challenger_index(
        self,
        facts: list[FactRecord],
    ) -> dict[tuple[SeriesKey, str], FactRecord]:
        """Latest-vintage record per (SeriesKey, source_id). Spine and projections excluded."""
        index: dict[tuple[SeriesKey, str], FactRecord] = {}
        for f in facts:
            if f.source_id == SPINE_SOURCE_ID or f.is_projection:
                continue
            key = SeriesKey(
                f.geography, f.energy_type, f.metric, f.metric_family, f.period,
            )
            compound = (key, f.source_id)
            existing = index.get(compound)
            if existing is None or f.vintage > existing.vintage:
                index[compound] = f
        return index
