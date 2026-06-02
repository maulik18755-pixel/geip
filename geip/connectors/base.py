"""Connector contract.

Every data source (OWID, EIA, Ember, ...) implements this protocol. The
ingestion scheduler treats all sources uniformly through it.

Lifecycle per poll:
    raw   = connector.fetch(since=last_vintage)
    facts = connector.normalize(raw)
    report = connector.validate(facts)
    -> upsert facts, update freshness manifest
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Protocol, runtime_checkable

from geip.core.schema import FactRecord


@dataclass
class Cadence:
    """Declares how often a source publishes, so the scheduler can poll it."""
    label: str          # "weekly" | "twice_monthly" | "annual" | "irregular"
    poll_hours: int     # how often to CHECK for new data


@dataclass
class ValidationReport:
    source_id: str
    n_facts: int
    n_errors: int
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.n_errors == 0


@runtime_checkable
class SourceConnector(Protocol):
    source_id: str
    cadence: Cadence
    license: str

    def fetch(self, since: Optional[date]) -> Any:
        """Pull raw payload, optionally only data newer than `since` vintage."""
        ...

    def normalize(self, raw: Any) -> list[FactRecord]:
        """Convert raw payload into canonical FactRecords (units normalized)."""
        ...

    def validate(self, facts: list[FactRecord]) -> ValidationReport:
        """Check schema/unit/coverage. Never mutate; only report."""
        ...
